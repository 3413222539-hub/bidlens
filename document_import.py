import http.client
import ipaddress
import json
import socket
import time
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import urlopen
from uuid import uuid4

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

from mineru_pdf import parse_pdf
from retrieve_evidence import DATA


IMPORTS = DATA / "imports"
MAX_BYTES = 200_000_000
MINERU_MAX_PAGES = 200
CHUNK_PAGES = 150
FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")


def write_job(path, job):
    staging = path.with_name(f"{path.stem}-{uuid4().hex}.tmp")
    try:
        staging.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        for attempt in range(6):
            try:
                staging.replace(path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.025 * (attempt + 1))
    finally:
        staging.unlink(missing_ok=True)


def save_job(folder, job):
    try:
        write_job(folder / "job.json", job)
        (folder / "job-terminal.json").unlink(missing_ok=True)
    except PermissionError:
        if job.get("state") != "failed" and job.get("risk_state") not in {"failed", "ready"}:
            raise
        # A reader or scanner may still hold the old file open on Windows.
        write_job(folder / "job-terminal.json", job)


def read_job(folder):
    for filename in ("job-terminal.json", "job.json"):
        try:
            return json.loads((folder / filename).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
    return None


def copy_pdf(source, destination, deadline=None):
    header = source.read(1024)
    if b"%PDF-" not in header:
        raise ValueError("没有收到 PDF，请检查文件或使用可直接下载的 PDF 链接")
    read = getattr(source, "read1", source.read)
    size = len(header)
    with destination.open("xb") as output:
        output.write(header)
        while chunk := read(1024 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("PDF 超过 200 MB")
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("下载 PDF 超时")
            output.write(chunk)


def public_url(url):
    try:
        parts = urlsplit(url)
        port = 443 if parts.scheme == "https" else 80
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.port not in {None, port}
                or any(char.isspace() for char in url)):
            raise ValueError
    except ValueError:
        raise ValueError("请输入公开的 HTTP(S) PDF 直链") from None
    return parts, port


def resolve_public_ips(hostname, port):
    records = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(ipaddress.ip_address(record[4][0]) for record in records))
    if addresses and all(ip in FAKE_IP_RANGE for ip in addresses):
        # Some local VPNs return synthetic DNS addresses for public domains.
        try:
            for record_type in (1, 28):
                query = urlencode({"name": hostname, "type": record_type})
                with urlopen(f"https://dns.google/resolve?{query}", timeout=10) as response:
                    answers = json.load(response).get("Answer", [])
                addresses = [ipaddress.ip_address(answer["data"]) for answer in answers if answer["type"] == record_type]
                if addresses:
                    break
        except (OSError, ValueError):
            raise ValueError("无法取得链接的公网 DNS 地址，请检查代理或改用文件上传") from None
    if not addresses or any(not ip.is_global or ip.is_multicast for ip in addresses):
        raise ValueError("链接必须指向公网，无法导入本机或内网地址")
    return addresses


def download_pdf(url, destination):
    deadline = time.monotonic() + 120
    for _ in range(5):
        parts, port = public_url(url)
        addresses = resolve_public_ips(parts.hostname, port)
        connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parts.hostname, port, timeout=20)

        # Connect to the validated IPs and keep the original HTTP and TLS hostname.
        def connect_public(address, timeout, source_address):
            for ip in addresses:
                try:
                    return socket.create_connection((str(ip), address[1]), timeout, source_address)
                except OSError as error:
                    last_error = error
            raise last_error

        connection._create_connection = connect_public
        target = quote(parts.path or "/", safe="/%:@!$&'()*+,;=")
        if parts.query:
            target += "?" + quote(parts.query, safe="%/?@:!$&'()*+,;=")
        try:
            connection.request("GET", target, headers={"Accept": "application/pdf, */*", "User-Agent": "TenderWorkbench/1.0"})
            with connection.getresponse() as response:
                location = response.getheader("Location")
                if response.status in {301, 302, 303, 307, 308} and location:
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise ValueError(f"PDF 下载失败（HTTP {response.status}）")
                copy_pdf(response, destination, deadline)
                return
        finally:
            connection.close()
    raise ValueError("PDF 链接跳转次数过多")


def pdf_name(value):
    name = PurePosixPath(value.replace("\\", "/")).name[:200]
    return name if name.lower().endswith(".pdf") else (name or "导入文档") + ".pdf"


def create_job(name):
    doc_id = uuid4().hex
    folder = IMPORTS / doc_id
    folder.mkdir(parents=True)
    return {"id": doc_id, "name": name, "state": "queued"}, folder


def parse_document(pdf, reader, pages, job):
    if pages <= MINERU_MAX_PAGES:
        save_job(pdf.parent, {**job, "state": "parsing", "pages": pages})
        return parse_pdf(pdf)

    evidence = []
    with TemporaryDirectory(dir=pdf.parent) as chunks:
        for start in range(0, pages, CHUNK_PAGES):
            end = min(start + CHUNK_PAGES, pages)
            save_job(pdf.parent, {**job, "state": "parsing", "page_start": start + 1, "page_end": end, "pages": pages})
            writer = PdfWriter()
            for index in range(start, end):
                writer.add_page(reader.pages[index])
            chunk = Path(chunks) / f"pages-{start + 1}-{end}.pdf"
            with chunk.open("wb") as output:
                writer.write(output)
            segment = parse_pdf(chunk, first_page=start + 1)
            if not segment or any(not start < block["page"] <= end for block in segment):
                raise ValueError(f"第 {start + 1}–{end} 页没有返回有效证据，导入未完成")
            for block in segment:
                block["evidence_id"] = f"E{len(evidence) + 1:04d}"
                evidence.append(block)
    return evidence


def run_import(job, folder, url=None):
    pdf = folder / "document.pdf"
    evidence_path = folder / "evidence.json"
    metadata = folder / "document.json"
    staging = metadata.with_suffix(".tmp")
    try:
        if url:
            save_job(folder, {**job, "state": "downloading"})
            download_pdf(url, pdf)
        with pdf.open("rb") as source:
            reader = PdfReader(source)
            pages = len(reader.pages)
            if not pages:
                raise ValueError("PDF 没有可解析的页面")
            evidence = parse_document(pdf, reader, pages, job)
        if not evidence:
            raise ValueError("MinerU 没有返回可检索的证据")
        if any(not 1 <= block["page"] <= pages for block in evidence):
            raise ValueError("解析结果页码超出原 PDF 范围")
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        ready = {**job, "state": "ready", "pages": pages}
        staging.write_text(json.dumps(ready, ensure_ascii=False), encoding="utf-8")
        staging.replace(metadata)
        return ready
    except PyPdfError:
        message = "PDF 无法读取，请检查文件是否损坏或加密"
    except TimeoutError:
        message = "处理超时，请检查链接或稍后重试"
    except HTTPError as error:
        message = f"MinerU 请求返回 HTTP {error.code}，请检查服务状态和 API Token"
    except (ValueError, RuntimeError) as error:
        message = str(error)
    except Exception:
        message = "下载或调用 MinerU 失败，请检查网络和 MinerU 配置"
    save_job(folder, {**job, "state": "failed", "error": message})
    for path in (pdf, evidence_path, staging):
        path.unlink(missing_ok=True)


def url_name(url):
    parts, _ = public_url(url)
    return pdf_name(unquote(parts.path.rsplit("/", 1)[-1]))
