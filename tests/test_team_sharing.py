import json
import secrets
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

import uvicorn

import team_store
import web_app


def test_team_access_concurrent_confirmations_and_shared_correction(tmp_path, monkeypatch):
    monkeypatch.setattr(team_store, "DB", tmp_path / "test.sqlite3")
    monkeypatch.setattr(web_app, "TEAM", True)
    imports = tmp_path / "imports"
    monkeypatch.setattr(web_app, "IMPORTS", imports)
    monkeypatch.setattr(web_app, "comparison_files", lambda doc: (imports / doc, ([], [])))
    team_store.initialize()
    password = secrets.token_urlsafe(20)
    for name in ("owner", "reviewer"):
        team_store.add_user(name, password)
    original, correction = "a" * 32, "b" * 32
    for doc in (original, correction):
        folder = imports / doc
        folder.mkdir(parents=True)
        (folder / "document.json").write_text(json.dumps({"id": doc, "name": doc}), encoding="utf-8")
        (folder / "document.pdf").write_bytes(b"%PDF-1.4")
        team_store.own(doc, "owner")
    (imports / original / "review_state.json").write_text(
        json.dumps({"raw": {"legacy": {"by": "original-reviewer", "at": ""}}}), encoding="utf-8")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(web_app.app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    def request(client, path, method="GET", body=None):
        req = Request(base + path, method=method, headers={"Content-Type": "application/json"},
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with client.open(req, timeout=10) as response:
                return response.status, json.load(response) if "json" in response.headers.get("Content-Type", "") else response.read()
        except HTTPError as error:
            return error.code, None

    owner, reviewer = [build_opener(HTTPCookieProcessor(CookieJar())) for _ in range(2)]
    endpoint = f"/api/documents/{original}"
    try:
        assert request(owner, endpoint + "/pdf")[0] == 401
        monkeypatch.setattr(team_store.secrets, "choice", lambda _: "A")
        newcomer = build_opener(HTTPCookieProcessor(CookieJar()))

        def captcha():
            status, challenge = request(newcomer, "/api/session/captcha", "POST")
            assert status == 200 and challenge["image"].startswith("data:image/png;base64,iVBOR")
            return challenge["captcha_id"]

        def register(token, code="AAAAA", name="newcomer"):
            return request(newcomer, "/api/session/register", "POST",
                           {"name": name, "password": password, "captcha_id": token, "captcha_code": code})[0]

        wrong = captcha()
        assert register(wrong, "WRONG") == 422
        assert register(wrong) == 422
        expired = captcha()
        with team_store.connect() as db:
            db.execute("UPDATE registration_codes SET expires=0 WHERE token=?", (expired,))
        assert register(expired) == 422
        fresh = captcha()
        assert register(fresh, "aaaaa") == 201
        assert register(fresh, name="replay") == 422
        assert register(captcha()) == 409
        status, result = request(newcomer, "/api/session/login", "POST", {"name": "newcomer", "password": password})
        assert status == 200 and not result["user"]["admin"]
        for name, client in (("owner", owner), ("reviewer", reviewer)):
            assert request(client, "/api/session/login", "POST", {"name": name, "password": password})[0] == 200
        assert request(reviewer, "/api/documents") == (200, [])
        assert request(reviewer, endpoint + "/pdf")[0] == 404
        assert request(owner, endpoint + "/shares", "PUT", {"name": "reviewer"})[0] == 200
        assert request(reviewer, endpoint + "/pdf")[0] == 200
        assert request(reviewer, endpoint + "/shares", "PUT", {"name": "owner"})[0] == 403
        assert request(owner, endpoint + "/correction", "PUT", {"correction_id": correction})[0] == 200
        assert request(reviewer, endpoint + "/correction") == (200, {"correction_id": ""})
        assert request(reviewer, "/api/ask", "POST",
                       {"document_id": original, "correction_id": correction, "question": "限价改了吗？"})[0] == 404
        assert request(owner, f"/api/documents/{correction}/shares", "PUT", {"name": "reviewer"})[0] == 200
        assert request(reviewer, endpoint + "/correction") == (200, {"correction_id": correction})
        assert request(reviewer, "/api/ask", "POST",
                       {"document_id": original, "correction_id": correction, "question": "限价改了吗？"}) == (
            200, {"status": "comparison", "changes": []})

        def confirm(client, item):
            return request(client, endpoint + "/confirmations", "POST",
                           {"scope": "raw", "item_id": item, "confirmed": True, "reviewer": "伪造名字"})

        with ThreadPoolExecutor() as pool:
            tasks = [pool.submit(confirm, owner, "risk-1"), pool.submit(confirm, reviewer, "risk-2")]
            assert [task.result()[0] for task in tasks] == [200, 200]
        state = request(owner, endpoint + "/confirmations")[1]["raw"]
        assert {key: entry["by"] for key, entry in state.items()} == {
            "legacy": "original-reviewer", "risk-1": "owner", "risk-2": "reviewer",
        }
        assert request(owner, endpoint + "/shares", "PUT", {"name": "reviewer", "grant": False})[0] == 200
        assert request(reviewer, endpoint + "/confirmations")[0] == 404
        status, groups = request(owner, "/api/groups", "POST", {"title": "复核组"})
        assert status == 200
        group, = groups
        assert request(reviewer, "/api/groups") == (200, [])
        assert request(reviewer, "/api/groups/join", "POST", {"invite": "invalid"})[0] == 404
        for doc in (original, correction):
            assert request(owner, f"/api/documents/{doc}/groups", "PUT", {"group_id": group["id"]})[0] == 200
        assert request(reviewer, endpoint + "/pdf")[0] == 404
        status, joined = request(reviewer, "/api/groups/join", "POST", {"invite": group["invite"]})
        assert status == 200 and joined[0]["invite"] is None
        assert request(reviewer, endpoint + "/pdf")[0] == 200
        assert request(reviewer, endpoint + "/correction") == (200, {"correction_id": correction})
        assert request(reviewer, endpoint + "/groups", "PUT", {"group_id": group["id"]})[0] == 403
        assert request(reviewer, f"/api/groups/{group['id']}", "DELETE") == (200, [])
        assert request(reviewer, endpoint + "/pdf")[0] == 404
        assert request(reviewer, "/api/groups/join", "POST", {"invite": group["invite"]})[0] == 200
        assert request(owner, endpoint + "/groups", "PUT", {"group_id": group["id"], "grant": False})[0] == 200
        assert request(reviewer, endpoint + "/pdf")[0] == 404
        assert request(reviewer, "/api/session/logout", "POST")[0] == 200
        assert request(reviewer, "/api/documents")[0] == 401
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
