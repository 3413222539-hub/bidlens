import json
import base64
import io
import logging
import os
import secrets
import sqlite3
import re
from concurrent.futures import Future
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache, wraps
from hashlib import sha256
from pathlib import Path
from threading import Lock
from time import perf_counter
from urllib.error import HTTPError, URLError

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from checklist_export import render_checklist
from company_profile import delete_profile, list_profiles, load_profile, match_checklist, save_profile
from document_compare import (
    DEADLINE, LIMIT, QUALIFICATION, compare_documents, comparison_request, original_deadlines,
    original_package_limits, project_ids,
)
from document_import import IMPORTS, MAX_BYTES, copy_pdf, create_job, pdf_name, read_job, run_import, save_job, url_name
from package_scoring import package_score_answer, scoring_scope
from package_subcontract import answer_subcontract, subcontract_question
from package_review import answer_package_fact, checklist_card, package_checklist, read_package_facts
from retrieve_evidence import DATA, DOCUMENT, units_from
from risk_review import extract_items
from tender_assistant import agent_messages_json, ask_model, build_search, citation, find_candidates, load_config, read_document_inputs, resolve_answer
from tender_agent import run_agent
import team_store
from revision_chain import compare_chain, effective_time


WEB = Path(__file__).with_name("web")
SAMPLE = {"id": "sample", "name": "网络安全服务项目（样例）", "pages": 158, "state": "ready"}
TEAM = os.getenv("TENDER_TEAM") == "1"
SESSION_COOKIE = "tender_team_session"


@asynccontextmanager
async def lifespan(app):
    if TEAM:
        team_store.initialize()
    for path in IMPORTS.glob("*/job.json"):
        try:
            job = read_job(path.parent)
            if not job:
                continue
            metadata = path.with_name("document.json")
            if job["state"] in {"queued", "downloading", "parsing"}:
                if metadata.is_file():
                    job = json.loads(metadata.read_text(encoding="utf-8"))
                    job.update(risk_state="failed", risk_error="服务重启，请手动生成清单。")
                else:
                    job.update(state="failed", error="服务重启导致解析中断，请重新提交文件。")
                save_job(path.parent, job)
            elif job.get("risk_state") == "running":
                save_job(path.parent, {**job, "risk_state": "failed", "risk_error": "服务重启，请重试生成清单。"})
        except (OSError, ValueError, KeyError):
            logging.getLogger(__name__).warning("无法恢复导入任务 %s", path.parent.name)
    yield


app = FastAPI(title="标书智查", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=WEB), name="assets")
RISK_LOCK = Lock()
RISK_RUNNING = set()
CONFIRM_LOCK = Lock()
SCOPE = re.compile(r"^(?:package-(?:[0-9]{1,3}|general)|raw)$")


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    document_id: str = "sample"
    previous_question: str = Field(default="", max_length=500)
    correction_id: str = Field(default="", max_length=32)
    use_revision_chain: bool = False


class ImportLink(BaseModel):
    url: str = Field(min_length=1, max_length=4096)


class Confirmation(BaseModel):
    scope: str = Field(min_length=1, max_length=40)
    item_id: str = Field(min_length=1, max_length=200)
    confirmed: bool
    reviewer: str = Field(default="", max_length=40)


class ConfirmationBatch(BaseModel):
    scope: str = Field(min_length=1, max_length=40)
    item_ids: list[str] = Field(min_length=1, max_length=500)
    confirmed: bool
    reviewer: str = Field(default="", max_length=40)


class Login(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class Registration(Login):
    captcha_id: str = Field(min_length=1, max_length=64)
    captcha_code: str = Field(min_length=1, max_length=12)


class Share(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    grant: bool = True


class CorrectionLink(BaseModel):
    correction_id: str = Field(default="", max_length=32)


class RevisionEntry(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    effective_at: str


class RevisionSet(BaseModel):
    entries: list[RevisionEntry] = Field(max_length=30)
    version: str = Field(min_length=64, max_length=64)


class GroupTitle(BaseModel):
    title: str = Field(min_length=1, max_length=60)


class GroupInvite(BaseModel):
    invite: str = Field(min_length=1, max_length=64)


class GroupShare(BaseModel):
    group_id: int = Field(gt=0)
    grant: bool = True


@app.middleware("http")
async def team_access(request: Request, call_next):
    path = request.url.path
    if not TEAM or (not path.startswith("/api/") and path != "/document.pdf"):
        return await call_next(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse({"detail": "请求来源不匹配"}, 403)
    if path in {"/api/session", "/api/session/login", "/api/session/register", "/api/session/captcha"}:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
    user = team_store.identity(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"detail": "请先登录团队工作台"}, 401)
    request.state.user = user
    match = re.match(r"^/api/(?:documents|imports)/([0-9a-f]{32})(?:/|$)", path)
    targets = [match[1]] if match else []
    if path == "/document.pdf" or path == "/api/review" or path.startswith("/api/review/"):
        targets.append("sample")
    if path == "/api/ask" and request.method == "POST":
        try:
            payload = await request.json()
            question = Question.model_validate(payload)
        except ValueError:
            return JSONResponse({"detail": "问答参数无效"}, 422)
        targets.extend(filter(None, (question.document_id, question.correction_id)))
        if re.fullmatch(r"[0-9a-f]{32}", question.document_id):
            targets.extend(row['document_id'] for row in team_store.revisions(question.document_id)['entries'])
    if any(not team_store.can_access(user, doc) for doc in targets):
        return JSONResponse({"detail": "未找到可访问的文档"}, 404)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/session")
def session(request: Request):
    user = team_store.identity(request.cookies.get(SESSION_COOKIE)) if TEAM else None
    return {"team": TEAM, "user": user}


@app.post("/api/session/login")
def sign_in(payload: Login, request: Request, response: Response):
    if not TEAM:
        raise HTTPException(404, "团队模式未启用")
    token = team_store.login(payload.name.strip(), payload.password)
    if not token:
        raise HTTPException(401, "账号或密码错误")
    response.set_cookie(SESSION_COOKIE, token, max_age=team_store.TTL, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict")
    return {"user": team_store.identity(token)}


@app.post("/api/session/captcha")
def registration_captcha():
    if not TEAM:
        raise HTTPException(404, "团队模式未启用")
    from PIL import Image, ImageDraw, ImageFont

    token, code = team_store.issue_registration_code()
    rng = secrets.SystemRandom()
    image = Image.new("RGB", (182, 56), "#edf2f7")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=31)
    for _ in range(9):
        draw.line((rng.randrange(182), rng.randrange(56), rng.randrange(182), rng.randrange(56)),
                  fill="#a2b2c6", width=1)
    for index, character in enumerate(code):
        glyph = Image.new("RGBA", (34, 45))
        ImageDraw.Draw(glyph).text((4, 3), character, font=font, fill="#16283d")
        glyph = glyph.rotate(rng.randint(-19, 19), resample=Image.Resampling.BICUBIC)
        image.paste(glyph, (8 + index * 33, rng.randint(3, 8)), glyph)
    png = io.BytesIO()
    image.save(png, "PNG")
    return {"captcha_id": token, "image": "data:image/png;base64," + base64.b64encode(png.getvalue()).decode("ascii")}


@app.post("/api/session/register", status_code=201)
def register_account(payload: Registration):
    if not TEAM:
        raise HTTPException(404, "团队模式未启用")
    if not team_store.verify_registration_code(payload.captcha_id, payload.captcha_code):
        raise HTTPException(422, "验证码错误、已使用或已过期，请刷新后重试")
    try:
        team_store.add_user(payload.name.strip(), payload.password)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该账号已存在") from None
    return {"ok": True}


@app.post("/api/session/logout")
def sign_out(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if TEAM and token:
        team_store.logout(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


class Qualification(BaseModel):
    type: str = Field(min_length=1, max_length=120)
    valid_until: str = Field(default="", max_length=20)


class CompanyProfile(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    credit_code: str = Field(default="", max_length=40)
    qualifications: list[Qualification] = Field(default_factory=list)
    certifications: list[Qualification] = Field(default_factory=list)
    performance: list[dict] = Field(default_factory=list)
    notes: str = Field(default="", max_length=1000)


def import_folder(document_id):
    if not re.fullmatch(r"[0-9a-f]{32}", document_id):
        raise HTTPException(404, "没有找到这份文档")
    return IMPORTS / document_id


def ready_folder(document_id):
    folder = import_folder(document_id)
    if not (folder / "document.json").is_file():
        raise HTTPException(404, "没有找到已解析的文档")
    return folder


def stamp(path):
    """文件指纹：mtime + 大小。文件被替换或改写时指纹变化，用于让缓存失效。"""
    try:
        stat = Path(path).stat()
    except OSError:
        return 0
    return stat.st_mtime_ns, stat.st_size


def cached_build(maxsize):
    """Share a cache miss across threads calling with the same version key."""
    def decorate(build):
        cached = lru_cache(maxsize=maxsize)(build)
        building = {}
        lock = Lock()

        @wraps(build)
        def load(*key):
            with lock:
                pending = building.get(key)
                owner = pending is None
                if owner:
                    pending = building[key] = Future()
            if not owner:
                return pending.result()
            try:
                result = cached(*key)
            except BaseException as error:
                pending.set_exception(error)
                raise
            else:
                pending.set_result(result)
                return result
            finally:
                with lock:
                    del building[key]

        load.cache_clear = cached.cache_clear
        return load
    return decorate


def invalidate_document(document_id):
    """Clear derived caches explicitly, for example when measuring a cold start."""
    for cached in (_document_inputs, _search_index, _checklist_facts, _review_data, _table_rows):
        cached.cache_clear()


# Version keys include both file fingerprints, so re-imports naturally use new inputs.
@cached_build(maxsize=6)
def _document_inputs(evidence, pdf, evidence_stamp, pdf_stamp):
    return read_document_inputs(evidence, pdf)


@cached_build(maxsize=16)
def _search_index(document_id, evidence_stamp, pdf_stamp):
    if document_id == "sample":
        evidence, pdf = DATA / f"{DOCUMENT}_evidence.json", DATA / f"{DOCUMENT}.pdf"
    else:
        folder = ready_folder(document_id)
        evidence, pdf = folder / "evidence.json", folder / "document.pdf"
    prepared = _document_inputs(evidence, pdf, evidence_stamp, pdf_stamp)
    return build_search(evidence, pdf, prepared=prepared)


def search_index(document_id):
    if document_id == "sample":
        return _search_index("sample", stamp(DATA / f"{DOCUMENT}_evidence.json"),
                             stamp(DATA / f"{DOCUMENT}.pdf"))
    folder = ready_folder(document_id)
    return _search_index(document_id,
                         stamp(folder / "evidence.json"),
                         stamp(folder / "document.pdf"))


def comparison_inputs(evidence, pdf, evidence_stamp, pdf_stamp):
    _, rows, pages = _document_inputs(evidence, pdf, evidence_stamp, pdf_stamp)
    return rows, pages


def comparison_files(document_id):
    folder = DATA if document_id == "sample" else ready_folder(document_id)
    evidence = folder / (f"{DOCUMENT}_evidence.json" if document_id == "sample" else "evidence.json")
    pdf = folder / (f"{DOCUMENT}.pdf" if document_id == "sample" else "document.pdf")
    if not evidence.is_file() or not pdf.is_file():
        raise HTTPException(404, "比对文件缺少 PDF 或解析结果")
    return folder, comparison_inputs(evidence, pdf, stamp(evidence), stamp(pdf))


@lru_cache(maxsize=8)
def _review_data(report_stamp, evidence_stamp):
    return json.loads((DATA / f"{DOCUMENT}_invalid_bid_report.json").read_text(encoding="utf-8-sig"))


def review_data():
    return _review_data(stamp(DATA / f"{DOCUMENT}_invalid_bid_report.json"),
                        stamp(DATA / f"{DOCUMENT}_evidence.json"))


@lru_cache(maxsize=8)
def _table_rows(evidence_stamp):
    blocks = json.loads((DATA / f"{DOCUMENT}_evidence.json").read_text(encoding="utf-8-sig"))
    return list(units_from((block for block in blocks if block["type"] == "table"), True))


def table_rows():
    return _table_rows(stamp(DATA / f"{DOCUMENT}_evidence.json"))


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/document.pdf")
def document():
    return FileResponse(DATA / f"{DOCUMENT}.pdf", media_type="application/pdf")


@app.get("/api/documents")
def documents(request: Request):
    imports = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(IMPORTS.glob("*/document.json"))]
    sample = (DATA / f"{DOCUMENT}.pdf").is_file() and (DATA / f"{DOCUMENT}_evidence.json").is_file()
    result = [SAMPLE, *imports] if sample else imports
    return [doc for doc in result if team_store.can_access(request.state.user, doc["id"])] if TEAM else result


@app.post("/api/imports/upload", status_code=202)
def upload_pdf(file: UploadFile, tasks: BackgroundTasks, request: Request):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(422, "请选择 PDF 文件")
    if file.size is not None and file.size > MAX_BYTES:
        raise HTTPException(413, "PDF 超过 200 MB")
    job, folder = create_job(pdf_name(file.filename))
    pdf = folder / "document.pdf"
    try:
        copy_pdf(file.file, pdf)
    except (ValueError, OSError) as error:
        pdf.unlink(missing_ok=True)
        folder.rmdir()
        if isinstance(error, ValueError):
            raise HTTPException(422, str(error)) from None
        raise HTTPException(500, "无法保存上传的 PDF") from None
    save_job(folder, job)
    if TEAM:
        team_store.own(job["id"], request.state.user["name"])
    tasks.add_task(import_and_review, job, folder)
    return job


@app.post("/api/imports/url", status_code=202)
def import_url(payload: ImportLink, tasks: BackgroundTasks, request: Request):
    url = payload.url.strip()
    try:
        name = url_name(url)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    job, folder = create_job(name)
    save_job(folder, job)
    if TEAM:
        team_store.own(job["id"], request.state.user["name"])
    tasks.add_task(import_and_review, job, folder, url)
    return job


@app.get("/api/imports/{document_id}")
def import_status(document_id: str):
    folder = import_folder(document_id)
    job = read_job(folder)
    if job is not None:
        return job
    metadata = folder / "document.json"
    if metadata.is_file():
        return json.loads(metadata.read_text(encoding="utf-8"))
    raise HTTPException(404, "找不到导入任务，服务重启后请重新提交未完成的文件")


@app.get("/api/documents/{document_id}/pdf")
def imported_pdf(document_id: str):
    return FileResponse(ready_folder(document_id) / "document.pdf", media_type="application/pdf")


@app.get("/api/documents/{document_id}/risks")
def get_risks(document_id: str):
    folder = ready_folder(document_id)
    report = folder / "risk_report.json"
    if report.is_file():
        return json.loads(report.read_text(encoding="utf-8"))
    job = read_job(folder) or {}
    running = document_id in RISK_RUNNING or job.get("risk_state") == "running" or job.get("state") == "parsing"
    return {"items": None, "risk_state": "running" if running else job.get("risk_state"),
            "risk_error": job.get("risk_error")}


@cached_build(maxsize=16)
def _checklist_facts(document_id, evidence_stamp, pdf_stamp):
    folder = ready_folder(document_id)
    original = comparison_inputs(folder / "evidence.json", folder / "document.pdf", evidence_stamp, pdf_stamp)
    packages, deposits, limits, licenses, methods = read_package_facts(folder, original)
    tabular_limits = list(original_package_limits(original[0]))
    if tabular_limits:
        packages = packages | {package for scope, _, _ in tabular_limits for package in scope}
        limits = [*limits, *((sources, dict.fromkeys(scope, amount))
                            for scope, (_, amount), sources in tabular_limits)]
    return packages, deposits, limits, licenses, methods


def checklist_facts(document_id):
    folder = ready_folder(document_id)
    return _checklist_facts(document_id,
                            stamp(folder / "evidence.json"),
                            stamp(folder / "document.pdf"))


def checklist_deadline(original, packages, package):
    if package is None and packages:
        return None
    rows = original_deadlines(*original)
    explicit = [row for row in rows if package in row[0]]
    matches = explicit or [row for row in rows if not row[0]]
    dates = sorted({value[1] for _, value, _ in matches})
    if dates:
        return checklist_card("bid-deadline", DEADLINE, "applies" if len(dates) == 1 else "pending",
                              "投标截止时间 " + "／".join(dates) + "。",
                              "核对递交时间，并在截止前提交投标文件。",
                              [source for _, _, sources in matches for source in sources])
    return None


def saved_revision_chain(document_id, request=None):
    if not re.fullmatch(r'[0-9a-f]{32}', document_id):
        return None
    entries = team_store.revisions(document_id)['entries']
    if not entries:
        return None
    if TEAM and request is not None and any(not team_store.can_access(request.state.user, e['document_id']) for e in entries):
        raise HTTPException(403, '需要获得所有关联公告的访问权限，才能核对当前有效条款')
    _, original = comparison_files(document_id)
    return compare_chain(checklist_facts(document_id), original, document_id, entries,
                         lambda key: comparison_files(key)[1])


def apply_chain_to_checklist(result, chain, package, confirmed):
    result['revision_timeline'] = chain['timeline']
    warnings = []
    for event in chain['timeline']:
        if event.get('reason'):
            warnings.append(event['reason'])
            if event['status'] == 'needs_review':
                for item in result['items']:
                    if item.get('base_item_id', item['id']) in {'price-limit', 'bid-deadline'} or item['id'].startswith('license-'):
                        item['review_blocked'] = event['reason']
                        item['applicability'] = 'pending'
        for change in event['changes']:
            if change['package'] != package:
                if change['package'] is None and change.get('reason'):
                    warnings.append(change['reason'])
                    for item in result['items']:
                        base = item.get('base_item_id', item['id'])
                        if (change['field'] == LIMIT and base == 'price-limit'
                            or change['field'] == DEADLINE and base == 'bid-deadline'
                            or change['field'] == QUALIFICATION and base.startswith('license-')):
                            item['review_blocked'] = change['reason']
                            item['applicability'] = 'pending'
                continue
            field = change['field']
            base_ids = ({'price-limit' if field == LIMIT else 'bid-deadline'} if field != QUALIFICATION else
                        {'license-' + ref['id'] for ref in change['original_sources']})
            matches = [item for item in result['items'] if item.get('base_item_id', item['id']) in base_ids
                       or field == QUALIFICATION and item.get('change_revision', {}).get('after') == change['before']]
            if change['status'] != 'changed':
                warnings.append(change.get('reason', '变更待核对'))
                for item in matches:
                    item['review_blocked'] = change.get('reason', '变更待核对')
                    item['applicability'] = 'pending'
                continue
            if not matches and field == QUALIFICATION:
                add_license_revision(result, change['notice_id'], change, change.get('action') == '新增')
                matches = [result['items'][-1]]
            for item in matches:
                base = item.get('base_item_id', item['id'].split(':')[0])
                history = item.setdefault('revision_history', [])
                previous_id = item['id']
                history.append({**change, 'previous_item_id': previous_id,
                                'previous_confirmation': confirmed.get(previous_id)})
                item['id'] = base
                item['base_item_id'] = base
                attach_revision(item, change['notice_id'], change, confirmed)
                history[-1]['item_id'] = item['id']
    result['revision_warnings'] = list(dict.fromkeys(warnings))


@app.get('/api/documents/{document_id}/revisions')
@app.put('/api/documents/{document_id}/revisions')
def document_revisions(document_id: str, request: Request, payload: RevisionSet | None = None):
    ready_folder(document_id)
    if request.method == 'PUT':
        if TEAM and not team_store.owns(request.state.user, document_id):
            raise HTTPException(403, '只有文档所有者或管理员可修改公告时间线')
        if payload is None:
            raise HTTPException(422, '缺少公告列表')
        entries, seen = [], set()
        for entry in payload.entries:
            target = entry.document_id
            if target == document_id or target in seen:
                raise HTTPException(422, '不能关联自身或重复公告')
            seen.add(target)
            if TEAM and not team_store.can_access(request.state.user, target):
                raise HTTPException(404, '未找到可访问的公告')
            ready_folder(target)
            try:
                entries.append({'document_id': target, 'effective_at': effective_time(entry.effective_at)})
            except ValueError:
                raise HTTPException(422, '请输入有效的公告生效时间（包含时区）') from None
        entries.sort(key=lambda e: (e['effective_at'], e['document_id']))
        try:
            team_store.revisions(document_id, entries, payload.version,
                                 request.state.user['name'] if TEAM else '本地用户')
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
    saved = team_store.revisions(document_id)
    if TEAM and any(not team_store.can_access(request.state.user, e['document_id']) for e in saved['entries']):
        raise HTTPException(403, '需要所有关联公告的访问权限')
    chain = saved_revision_chain(document_id, request)
    return {**saved, 'timeline': chain['timeline'] if chain else [],
            'has_conflicts': chain['has_conflicts'] if chain else False,
            'can_edit': not TEAM or team_store.owns(request.state.user, document_id)}


def revision_key(base, correction_id, change):
    # Keep prior price confirmation IDs compatible.
    fingerprint = [correction_id, change["before"], change["after"]]
    if change.get("chain_revision"):
        fingerprint.append(change["chain_revision"])
    digest = sha256(json.dumps(fingerprint, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"{base}:{digest}"


def attach_revision(item, correction_id, change, confirmed):
    base = item["id"]
    revision_id = revision_key(base, correction_id, change)
    earlier = [record for key, record in confirmed.items()
               if (key == base or key.startswith(base + ":")) and key != revision_id]
    item.update(id=revision_id, change_revision=change,
                previous_confirmation=max(earlier, key=lambda record: record.get("at") or "", default=None))
    if change["field"] == LIMIT:
        item.update(price_revision=change,
                    condition=f'最高限价由 {change["before"]} 调整为 {change["after"]}。',
                    check="对照澄清公告，按变更后的最高限价核对本标包报价。")
    elif change["field"] == DEADLINE:
        item.update(condition=f'投标截止时间由 {change["before"]} 调整为 {change["after"]}。',
                    check="对照澄清公告，按变更后的截止时间安排递交。")
    else:
        item.update(condition=change["after"],
                    check="对照原证照条款和澄清公告，仅按已明确变更的范围重新核对资格。")


def add_license_revision(result, correction_id, change, new=False):
    ref = change["correction_sources"][0]
    base = f"license-change-{ref['id']}"
    item_id = revision_key(base, correction_id, change)
    if any(row["id"] == item_id for row in result["items"]):
        return
    title = "新增证照资格要求" if new else "证照资格变更"
    check = ("按澄清公告准备新增证照。" if new
             else "未匹配到旧清单条目，请对照双方原文重新核对本项。")
    result["items"].append(checklist_card(item_id, title, "applies",
                                          change["after"], check, [], change_revision=change))


def checklist_revisions(result, facts, original, document_id, correction_id, package, confirmed):
    if not correction_id or correction_id == document_id:
        return
    _, correction = comparison_files(correction_id)
    original_ids, correction_ids = project_ids(original), project_ids(correction)
    if len(original_ids) == len(correction_ids) == 1 and original_ids.isdisjoint(correction_ids):
        result["revision_warnings"] = ["项目编号不同，未应用澄清公告。"]
        return
    changes = compare_documents(facts, original, correction, [LIMIT, DEADLINE, QUALIFICATION],
                                {package} if package is not None else set(),
                                document_id, correction_id)["changes"]
    by_item = {}
    warnings = []
    items_by_id = {item["id"]: item for item in result["items"]}
    for change in changes:
        field = change["field"]
        if change["package"] != package:
            if change["package"] is None and change["reason"]:
                warnings.append(f'{field}：{change["reason"]}')
            continue
        if change["status"] != "changed" or not change["original_sources"] or not change["correction_sources"]:
            if change["reason"]:
                warnings.append(f'{field}：{change["reason"]}')
            continue
        if field == QUALIFICATION:
            if change.get("action") == "新增":
                add_license_revision(result, correction_id, change, new=True)
                continue
            ids = {"license-" + source["id"] for source in change["original_sources"]}
        else:
            ids = {"price-limit" if field == LIMIT else "bid-deadline"}
        matching = ids & items_by_id.keys()
        if not matching:
            if field == QUALIFICATION:
                add_license_revision(result, correction_id, change)
            else:
                warnings.append(f"{field}：公告有明确变更，但未匹配到原清单条目，请查看公告比对结果。")
        for item_id in matching:
            by_item.setdefault(item_id, []).append(change)
    for item in result["items"]:
        affected = by_item.get(item["id"], [])
        if len(affected) == 1 and item["applicability"] != "excluded":
            attach_revision(item, correction_id, affected[0], confirmed)
        elif len(affected) > 1:
            warnings.append(f'{item["title"]}：该条目有多处变更，请逐条核对原文。')
    if warnings:
        result["revision_warnings"] = list(dict.fromkeys(warnings))


@app.get("/api/documents/{document_id}/checklist")
def get_checklist(document_id: str, request: Request, package: int | None = None,
                  correction_id: str = ""):
    report = get_risks(document_id)
    if report["items"] is None:
        raise HTTPException(409, "请先生成风险清单")
    facts = checklist_facts(document_id)
    if package is not None and package not in facts[0]:
        raise HTTPException(422, "未识别到这个标包")
    result = package_checklist(report, facts, package)
    _, original = comparison_files(document_id)
    if deadline := checklist_deadline(original, facts[0], package):
        result["items"].insert(0, deadline)
    chain = saved_revision_chain(document_id, request)
    if chain is not None:
        scope = f"package-{package if package is not None else 'general'}"
        apply_chain_to_checklist(result, chain, package, review_state(document_id).get(scope, {}))
        return result
    if TEAM:
        correction_id = team_store.correction(document_id)
        if correction_id and not team_store.can_access(request.state.user, correction_id):
            for item in result["items"]:
                if item["id"] in {"price-limit", "bid-deadline"} or item["id"].startswith("license-"):
                    item["review_blocked"] = "需获得澄清公告的访问权限，才能核对当前条款。"
            return result
    if correction_id:
        scope = f"package-{package if package is not None else 'general'}"
        checklist_revisions(result, facts, original, document_id, correction_id, package,
                            review_state(document_id).get(scope, {}))
    return result


def legacy_review_state(document_id):
    path = ready_folder(document_id) / "review_state.json"
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def review_state(document_id):
    legacy = legacy_review_state(document_id)
    return team_store.confirmations(document_id, legacy) if TEAM else legacy


def validate_chain_confirmation(document_id, scope, ids, request):
    if scope == 'raw' or not re.fullmatch(r'[0-9a-f]{32}', document_id):
        return
    if not team_store.revisions(document_id)['entries']:
        return
    package = None if scope == 'package-general' else int(scope.split('-')[1])
    current = {item['id']: item for item in get_checklist(document_id, request, package)['items']}
    if any(key not in current or current[key].get('review_blocked') for key in ids):
        raise HTTPException(409, '条款已变更或存在冲突，请刷新并核对最新公告后再确认')


@app.get("/api/documents/{document_id}/confirmations")
def get_confirmations(document_id: str):
    return review_state(document_id)


@app.post("/api/documents/{document_id}/confirmations")
def set_confirmation(document_id: str, payload: Confirmation, request: Request):
    if not SCOPE.fullmatch(payload.scope):
        raise HTTPException(422, "确认范围无效")
    if payload.confirmed:
        validate_chain_confirmation(document_id, payload.scope, [payload.item_id], request)
    if TEAM:
        return team_store.confirmations(document_id, legacy_review_state(document_id),
                                        (payload.scope, payload.item_id, request.state.user["name"], payload.confirmed))
    path = ready_folder(document_id) / "review_state.json"
    with CONFIRM_LOCK:
        state = review_state(document_id)
        scope = state.setdefault(payload.scope, {})
        if payload.confirmed:
            scope[payload.item_id] = {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "by": payload.reviewer.strip() or "共享",
            }
        else:
            scope.pop(payload.item_id, None)
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        staging.replace(path)
    return state


@app.post("/api/documents/{document_id}/confirmations/batch")
def set_confirmations_batch(document_id: str, payload: ConfirmationBatch, request: Request):
    """批量确认或撤销，用于一次性处理整组条款。"""
    if not SCOPE.fullmatch(payload.scope):
        raise HTTPException(422, "确认范围无效")
    ids = list(dict.fromkeys(payload.item_ids))[:500]
    if not ids:
        raise HTTPException(422, "没有需要处理的条目")
    if payload.confirmed:
        validate_chain_confirmation(document_id, payload.scope, ids, request)
    if TEAM:
        reviewer = request.state.user["name"]
        changes = [(payload.scope, item, reviewer, payload.confirmed) for item in ids]
        return team_store.confirmations(document_id, legacy_review_state(document_id), changes)
    path = ready_folder(document_id) / "review_state.json"
    with CONFIRM_LOCK:
        state = review_state(document_id)
        scope = state.setdefault(payload.scope, {})
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        by = payload.reviewer.strip() or "共享"
        for item in ids:
            if payload.confirmed:
                scope[item] = {"at": at, "by": by}
            else:
                scope.pop(item, None)
        staging = path.with_suffix(".tmp")
        staging.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        staging.replace(path)
    return state


@app.get("/api/documents/{document_id}/shares")
@app.put("/api/documents/{document_id}/shares")
def manage_shares(document_id: str, request: Request, payload: Share | None = None):
    if not TEAM:
        raise HTTPException(404, "团队模式未启用")
    ready_folder(document_id)
    if not team_store.owns(request.state.user, document_id):
        raise HTTPException(403, "文档所有者或管理员可管理授权")
    if request.method == "PUT" and payload is None:
        raise HTTPException(422, "请输入团队账号")
    try:
        return {"members": team_store.shares(document_id, payload.name.strip() if payload else None,
                                             payload.grant if payload else True)}
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@app.get("/api/groups")
def list_groups(request: Request):
    if not TEAM:
        raise HTTPException(404, "团队模式未启用")
    return team_store.groups_for(request.state.user["name"])


@app.post("/api/groups")
def create_group(payload: GroupTitle, request: Request):
    title = payload.title.strip()
    if not title:
        raise HTTPException(422, "请输入群组名称")
    list_groups(request)
    team_store.create_group(request.state.user["name"], title)
    return list_groups(request)


@app.post("/api/groups/join")
def join_group(payload: GroupInvite, request: Request):
    list_groups(request)
    if not team_store.join_group(request.state.user["name"], payload.invite.strip()):
        raise HTTPException(404, "邀请码无效")
    return list_groups(request)


@app.delete("/api/groups/{group_id}")
def leave_group(group_id: int, request: Request):
    list_groups(request)
    if not team_store.leave_group(request.state.user["name"], group_id):
        raise HTTPException(422, "无法退出该群组，创建者须留在群组中")
    return list_groups(request)


@app.get("/api/documents/{document_id}/groups")
@app.put("/api/documents/{document_id}/groups")
def manage_document_groups(document_id: str, request: Request, payload: GroupShare | None = None):
    if not TEAM:
        raise HTTPException(404, "团队模式未启用")
    ready_folder(document_id)
    user = request.state.user
    if not team_store.owns(user, document_id):
        raise HTTPException(403, "文档所有者或管理员可管理群组共享")
    if request.method == "PUT" and payload is None:
        raise HTTPException(422, "请选择群组")
    try:
        return team_store.document_groups(document_id, user["name"], payload.group_id if payload else None,
                                          payload.grant if payload else True)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@app.get("/api/documents/{document_id}/correction")
@app.put("/api/documents/{document_id}/correction")
def document_correction(document_id: str, request: Request, payload: CorrectionLink | None = None):
    if not TEAM:
        raise HTTPException(404, "团队模式未启用")
    ready_folder(document_id)
    user = request.state.user
    target = None
    if request.method == "PUT":
        if not team_store.owns(user, document_id):
            raise HTTPException(403, "文档所有者或管理员可修改共享关联")
        if payload is None:
            raise HTTPException(422, "缺少关联参数")
        target = payload.correction_id
        if target:
            if target == document_id:
                raise HTTPException(422, "请选择另一份文件")
            if not team_store.can_access(user, target):
                raise HTTPException(404, "未找到可访问的文档")
            ready_folder(target)
    saved = team_store.correction(document_id, target)
    return {"correction_id": saved if saved and team_store.can_access(user, saved) else ""}


@app.get("/api/documents/{document_id}/checklist/print")
def print_checklist(document_id: str, request: Request, package: int | None = None,
                    correction_id: str = ""):
    result = get_checklist(document_id, request, package, correction_id)
    folder = ready_folder(document_id)
    scope = f"package-{package if package is not None else 'general'}"
    meta = json.loads((folder / "document.json").read_text(encoding="utf-8"))
    confirmed = review_state(document_id).get(scope, {})
    return HTMLResponse(render_checklist(meta, result, package, confirmed))


@app.get("/api/companies")
def companies():
    return list_profiles()


@app.post("/api/companies", status_code=201)
def create_company(payload: CompanyProfile):
    from uuid import uuid4

    company_id = uuid4().hex
    save_profile(company_id, payload.model_dump())
    return {"id": company_id, **payload.model_dump()}


@app.get("/api/companies/{company_id}")
def company_detail(company_id: str):
    try:
        return {"id": company_id, **load_profile(company_id)}
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, str(error)) from None


@app.put("/api/companies/{company_id}")
def update_company(company_id: str, payload: CompanyProfile):
    try:
        return {"id": company_id, **save_profile(company_id, payload.model_dump())}
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, str(error)) from None


@app.delete("/api/companies/{company_id}")
def remove_company(company_id: str):
    try:
        return delete_profile(company_id)
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, str(error)) from None


@app.get("/api/documents/{document_id}/eligibility")
def eligibility(document_id: str, company: str, request: Request, package: int | None = None,
                correction_id: str = ""):
    """Match company credentials against the same version shown in the checklist."""
    try:
        profile = load_profile(company)
    except (ValueError, FileNotFoundError) as error:
        raise HTTPException(404, str(error)) from None
    result = get_checklist(document_id, request, package, correction_id)
    result["items"] = [item for item in result["items"] if item["title"] != DEADLINE]
    matched = match_checklist(result, profile)
    return {"company": {"id": company, "name": profile.get("name", "")},
            "package": package, "revision_warnings": result.get("revision_warnings", []), **matched}


@app.post("/api/documents/{document_id}/risks")
def generate_risks(document_id: str, force: bool = False):
    """生成风险清单。force=True 时丢弃已有清单重新分析。"""
    folder = ready_folder(document_id)
    report = folder / "risk_report.json"
    with RISK_LOCK:
        if report.is_file() and not force:
            return get_risks(document_id)
        if document_id in RISK_RUNNING:
            raise HTTPException(409, "这份文件正在生成风险清单")
        RISK_RUNNING.add(document_id)
    staging = report.with_suffix(".tmp")
    try:
        config = load_config()
        result = extract_items(folder / "evidence.json", folder / "document.pdf", config)
        # 清单变化会影响标包事实与资质比对，清掉按文档缓存的派生结果
        staging.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        staging.replace(report)
        return result
    except HTTPError as error:
        if error.code == 401:
            raise HTTPException(502, "模型认证失败（HTTP 401）。请核对项目 .env 中的 API Key 和接入地址。") from None
        raise HTTPException(502, f"模型接口返回 HTTP {error.code}，请稍后重试") from None
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        raise HTTPException(502, "生成清单失败，请检查模型配置或稍后重试") from None
    finally:
        staging.unlink(missing_ok=True)
        with RISK_LOCK:
            RISK_RUNNING.discard(document_id)


def import_and_review(job, folder, url=None):
    started = perf_counter()
    ready = run_import(job, folder, url)
    logger = logging.getLogger("uvicorn.error")
    logger.info("document_import document=%s state=%s elapsed_ms=%.0f",
                job["id"], "ready" if ready else "failed", (perf_counter() - started) * 1000)
    if ready is None:
        return
    try:
        save_job(folder, {**ready, "risk_state": "running"})
        generate_risks(job["id"])
        save_job(folder, {**ready, "risk_state": "ready"})
    except HTTPException as error:
        if error.status_code != 409:
            save_job(folder, {**ready, "risk_state": "failed", "risk_error": error.detail})
            return
        # 手动请求先拿到了生成锁：等它写完后按实际结果落状态
        current = read_job(folder) or {}
        if current.get("risk_state") != "running":
            has_report = (folder / "risk_report.json").is_file()
            save_job(folder, {**ready, "risk_state": "ready" if has_report else None})
    except Exception as error:
        logging.getLogger("uvicorn.error").error("自动生成清单失败，文档 %s，异常 %s",
                                                job["id"], type(error).__name__)
        save_job(folder, {**ready, "risk_state": "failed", "risk_error": "自动生成失败，可以手动重试。"})


@app.get("/api/review")
def review():
    return review_data()


@app.get("/api/review/{number}/references")
def references(number: str):
    item = next((item for item in review_data()["items"] if item["number"] == number), None)
    if item is None:
        raise HTTPException(404, "没有找到这条审阅记录")
    # Scope lookups to the review-standards table so numbered clauses from
    # other chapters do not accidentally satisfy a reference.
    titles = {"2.1": "资格审查标准", "6.1": "符合性审查标准"}
    results = []
    for ref in item["cross_references"]:
        prefix = f"{ref} | {titles.get(ref, '')} |"
        sources = [
            {"id": f"{row['evidence_id']}-R{row['row']}", "page": row["page"], "text": row["text"]}
            for row in table_rows() if row["text"].startswith(prefix)
        ] if ref in titles else []
        results.append({"number": ref, "title": titles.get(ref, ""), "sources": sources})
    return results


def scoped_followup(question, previous):
    if re.fullmatch(r"(?:那|那么)?(?:评分等级|评分档次|得分档次|各档得分)(?:怎么|如何)划分[？?]?", question):
        item = re.fullmatch(r"(.+?)最高(?:可)?得(?:几|多少)分[？?]?", previous)
        if item:
            return f"只查「{item[1]}」这一项评分：有哪些档次、各得几分、每档的条件是什么？", item[1]
    item = re.fullmatch(r"(.+?评价)最高(?:可)?得(?:几|多少)分[？?]?", question)
    return question, item[1] if item else ""


@app.post("/api/ask")
def ask(payload: Question):
    question = payload.question.strip()
    if not question:
        raise HTTPException(422, "请输入问题")
    previous = payload.previous_question.strip()
    comparison = comparison_request(question, previous)
    if payload.document_id != 'sample':
        chain = saved_revision_chain(payload.document_id)
        if chain is not None and (comparison or re.search(r"最高限价|投标截止|许可证|证照", question)):
            from document_compare import comparison_packages
            selected = comparison_packages(question + ' ' + previous)
            wanted = comparison[0] if comparison else [field for field, pattern in
                [(LIMIT, r'限价'), (DEADLINE, r'截止'), (QUALIFICATION, r'许可证|证照')] if re.search(pattern, question)]
            chain['changes'] = [row for row in chain['changes'] if row['field'] in wanted
                                and (not selected or row['package'] in selected or row['package'] is None)]
            if comparison or chain['changes'] or chain['has_conflicts']:
                chain['current'] = [row for row in chain['current'] if row['field'] in wanted
                                    and (not selected or row['package'] in selected or row['package'] is None)]
                return chain

    if comparison and payload.correction_id:
        if payload.document_id == payload.correction_id:
            raise HTTPException(422, "请选择另一份 PDF 作为澄清公告")
        folder, original = comparison_files(payload.document_id)
        _, correction = comparison_files(payload.correction_id)
        original_ids, correction_ids = project_ids(original), project_ids(correction)
        if len(original_ids) == len(correction_ids) == 1 and original_ids.isdisjoint(correction_ids):
            return {"status": "needs_review", "reason": "两份文件提取到的项目编号不同，请核对后重新关联。"}
        fields, packages = comparison
        facts = read_package_facts(folder, original)
        return compare_documents(facts, original, correction, fields, packages,
                                 payload.document_id, payload.correction_id, f"{question} {previous}")
    if comparison and (QUALIFICATION in comparison[0] or re.search(r"限价|投标截止|澄清公告|更正公告", question)):
        return {"status": "needs_review", "reason": "请先在问答区关联已导入的澄清公告 PDF，再查看两份文件的变更。"}
    if payload.document_id != "sample" and not previous and re.search(r"投标保证金|最高限价", question):
        answer = answer_package_fact(question, checklist_facts(payload.document_id))
        if answer:
            return answer
    if payload.document_id != "sample" and subcontract_question(question):
        _, (units, _) = comparison_files(payload.document_id)
        return answer_subcontract(question, units, previous)
    scoring = scoring_scope(question, previous)
    if scoring is not None and payload.document_id != "sample":
        _, (units, pages) = comparison_files(payload.document_id)
        # Only enter the structured route for documents with explicit package scoring headings.
        known_packages = checklist_facts(payload.document_id)[0]
        if answer := package_score_answer(units, pages, scoring, known_packages):
            return answer
    started = perf_counter()
    search = search_index(payload.document_id)
    indexed = perf_counter()
    try:
        config = load_config()
    except (OSError, ValueError):
        raise HTTPException(503, "模型配置不可用，请检查项目的 .env") from None

    agent = build_agent(payload.document_id, search)
    prepared = perf_counter()
    logging.getLogger("uvicorn.error").info("agent_start document=%s", payload.document_id)
    try:
        history = [{"question": previous}] if previous else None
        scoped_question, scoring_item = scoped_followup(question, previous)
        result = run_agent(agent, scoped_question, config, agent_messages_json,
                           history=history, scoring_item=scoring_item)
    except HTTPError as exc:
        if exc.code == 401:
            raise HTTPException(502, "模型认证失败（HTTP 401）。请核对项目 .env 中的 API Key 和接入地址。") from None
        raise HTTPException(502, f"模型接口返回 HTTP {exc.code}") from None
    except (URLError, TimeoutError, ValueError):
        raise HTTPException(502, "模型请求失败，请稍后重试") from None

    logging.getLogger("uvicorn.error").info(
        "agent_done document=%s status=%s steps=%d elapsed_ms=%.0f",
        payload.document_id, result.get("status"), len(result.get("steps", [])),
        (perf_counter() - started) * 1000)
    result["timing_ms"] = {
        "index": round((indexed - started) * 1000),
        "prepare": round((prepared - indexed) * 1000),
        "agent": round((perf_counter() - prepared) * 1000),
    }
    if result.get("status") != "answered":
        result.setdefault("tool_evidence", _agent_evidence(agent))
    return result


def _agent_evidence(agent, limit=3):
    """回答失败时，把 agent 实际查到的证据交给前端供人工核对。"""
    rows = []
    for ref, unit in list(agent.seen.items())[-limit:]:
        rows.append({"id": ref, "page": unit.get("page", 0),
                     "text": unit.get("text", ""),
                     "page_verified": unit.get("page_verified", True),
                     "origin": unit.get("origin", "mineru")})
    return rows


def build_agent(document_id, search):
    """为一份文档装配 agent：检索索引 + 标包事实 + 风险清单 + 分页文本。"""
    from tender_agent import DocumentAgent
    from package_review import read_package_facts

    facts = checklist = None
    pages = dict(enumerate(getattr(search, "pdf_pages", ()), 1))
    if document_id != "sample":
        try:
            facts = checklist_facts(document_id)
        except Exception:  # noqa: BLE001
            facts = None
        report = get_risks(document_id)
        if report.get("items"):
            checklist = report
    uncovered = getattr(search, "uncovered_pages", [])
    return DocumentAgent(search, facts=facts, checklist=checklist, pages=pages, uncovered=uncovered)
