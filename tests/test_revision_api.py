"""HTTP tests for shared timelines, stale confirmations and source permissions."""
import json
import secrets
import socket
import threading
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor

import uvicorn
import team_store
import web_app
from package_review import read_package_facts
from test_document_compare import evidence


def test_timeline_permissions_optimistic_lock_and_reconfirmation(tmp_path, monkeypatch):
    original, first, second = [c * 32 for c in 'abc']
    documents = {
        original: evidence((1, '最高限价：第1包50万元；第2包75万元')),
        first: evidence((1, '第2包最高限价由75万元调整为80万元。')),
        second: evidence((1, '第2包最高限价由80万元调整为85万元。')),
    }
    monkeypatch.setattr(web_app, 'TEAM', True)
    monkeypatch.setattr(web_app, 'IMPORTS', tmp_path / 'imports')
    monkeypatch.setattr(team_store, 'DB', tmp_path / 'team.sqlite3')
    monkeypatch.setattr(web_app, 'comparison_files', lambda doc: (tmp_path / doc, documents[doc]))
    monkeypatch.setattr(web_app, 'checklist_facts', lambda doc: read_package_facts(None, documents[doc]))
    team_store.initialize()
    password = secrets.token_urlsafe(18)
    for name in ('owner', 'reader'):
        team_store.add_user(name, password)
    for doc in documents:
        folder = web_app.IMPORTS / doc
        folder.mkdir(parents=True)
        (folder / 'document.json').write_text(json.dumps({'id': doc, 'name': '测试文档', 'pages': 1}), encoding='utf-8')
        (folder / 'risk_report.json').write_text(json.dumps({'items': [], 'candidate_count': 0}), encoding='utf-8')
        team_store.own(doc, 'owner')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0)); sock.listen()
    base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(web_app.app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    owner, reader = [build_opener(HTTPCookieProcessor(CookieJar())) for _ in range(2)]

    def call(client, path, method='GET', body=None):
        request = Request(base + path, method=method, headers={'Content-Type': 'application/json'},
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            with client.open(request, timeout=10) as response:
                return response.status, json.load(response) if 'json' in response.headers.get('Content-Type', '') else response.read().decode()
        except HTTPError as error:
            return error.code, None

    endpoint = f'/api/documents/{original}'
    entry = lambda doc, day: {'document_id': doc, 'effective_at': f'2020-01-{day:02}T00:00:00Z'}
    try:
        for name, client in (('owner', owner), ('reader', reader)):
            assert call(client, '/api/session/login', 'POST', {'name': name, 'password': password})[0] == 200
        assert call(reader, endpoint + '/revisions')[0] == 404
        assert call(owner, endpoint + '/shares', 'PUT', {'name': 'reader'})[0] == 200
        empty = call(owner, endpoint + '/revisions')[1]
        payload = {'entries': [entry(first, 1)], 'version': empty['version']}
        assert call(reader, endpoint + '/revisions', 'PUT', payload)[0] == 403
        status, saved = call(owner, endpoint + '/revisions', 'PUT', payload)
        assert status == 200
        assert call(owner, endpoint + '/revisions', 'PUT', payload)[0] == 409
        assert call(reader, endpoint + '/revisions')[0] == 403
        assert call(reader, endpoint + '/checklist?package=2')[0] == 403
        assert call(reader, '/api/ask', 'POST', {'document_id': original, 'question': '第2包最高限价多少？'})[0] == 404
        assert call(owner, f'/api/documents/{first}/shares', 'PUT', {'name': 'reader'})[0] == 200
        items = call(reader, endpoint + '/checklist?package=2')[1]['items']
        old_id = next(i['id'] for i in items if i.get('change_revision'))
        confirm = {'scope': 'package-2', 'item_id': old_id, 'confirmed': True}
        assert call(reader, endpoint + '/confirmations', 'POST', confirm)[0] == 200
        payload = {'entries': [entry(first, 1), entry(second, 2)], 'version': saved['version']}
        assert call(owner, endpoint + '/revisions', 'PUT', payload)[0] == 200
        assert call(owner, f'/api/documents/{second}/shares', 'PUT', {'name': 'reader'})[0] == 200
        assert call(reader, endpoint + '/confirmations', 'POST', confirm)[0] == 409
        item = next(i for i in call(reader, endpoint + '/checklist?package=2')[1]['items'] if i.get('change_revision'))
        assert item['previous_confirmation']['by'] == 'reader'
        assert len(item['revision_history']) == 2
        assert '85万元' in item['condition']
        assert '变更历史' in call(reader, endpoint + '/checklist/print?package=2')[1]
        # A notice changed in place invalidates the prior value chain, even if its ID is unchanged.
        documents[second] = evidence((1, '第2包最高限价由70万元调整为90万元。'))
        item = next(i for i in call(reader, endpoint + '/checklist?package=2')[1]['items'] if i.get('change_revision'))
        assert item['review_blocked']
        confirm['item_id'] = item['id']
        assert call(reader, endpoint + '/confirmations', 'POST', confirm)[0] == 409
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
