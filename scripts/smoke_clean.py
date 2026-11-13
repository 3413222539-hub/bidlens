"""Exercise HTTP startup/demo and optionally real import in a clean checkout.

Run with the clean checkout as cwd and an installed Python environment.
Credentials must be supplied by environment; they are never printed.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-pdf', type=Path)
    parser.add_argument('--base-url', help='Check an already running, seeded instance')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    if not args.base_url:
        subprocess.run([sys.executable, 'scripts/seed_revision_demo.py'], check=True)
    sys.path.insert(0, str(root))
    from scripts.seed_revision_demo import ORIGINAL_ID, NOTICE_ID
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    base = args.base_url.rstrip('/') if args.base_url else f'http://127.0.0.1:{port}'
    env = {**os.environ, 'TENDER_TEAM':'0'}
    report = {'checks':{}, 'import':'not_run'}
    log_path = args.output.with_suffix('.server.log')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    def request(path, body=None):
        req = Request(base+path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type':'application/json'})
        with urlopen(req, timeout=20) as r:
            return json.load(r) if 'json' in r.headers.get('Content-Type','') else r.read()
    with log_path.open('w',encoding='utf-8') as log:
        server = None if args.base_url else subprocess.Popen(
            [sys.executable, '-m', 'uvicorn', 'web_app:app', '--host', '127.0.0.1', '--port', str(port)],
            env=env, stdout=log, stderr=log)
        try:
            for _ in range(60):
                try:
                    request('/api/session'); break
                except OSError:
                    if server and server.poll() is not None: raise RuntimeError('Server exited')
                    time.sleep(.5)
            else: raise TimeoutError('Startup')
            report['checks']['startup'] = True
            report['checks']['homepage'] = '标书智查' in request('/').decode()
            answer = request('/api/ask', {'document_id':ORIGINAL_ID,'correction_id':NOTICE_ID,'question':'第2包有哪些变更？'})
            report['checks']['demo_changes'] = len(answer['changes']) == 3
            checklist = request(f'/api/documents/{ORIGINAL_ID}/checklist?package=2&correction_id={NOTICE_ID}')
            report['checks']['demo_reconfirmation'] = sum(bool(i.get('change_revision')) for i in checklist['items']) == 3
            report['checks']['export'] = b'<!doctype html>' in request(f'/api/documents/{ORIGINAL_ID}/checklist/print?package=2&correction_id={NOTICE_ID}').lower()
            if args.live_pdf:
                boundary = uuid4().hex
                payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="holdout.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
                           + args.live_pdf.read_bytes() + f'\r\n--{boundary}--\r\n'.encode())
                req = Request(base+'/api/imports/upload', data=payload,
                              headers={'Content-Type':f'multipart/form-data; boundary={boundary}'})
                with urlopen(req,timeout=60) as r: job=json.load(r)
                deadline=time.monotonic()+900
                while time.monotonic()<deadline:
                    job=request('/api/imports/'+job['id'])
                    if job.get('state')=='failed': raise RuntimeError('Import failed: '+str(job.get('error')))
                    if job.get('state')=='ready' and job.get('risk_state') in ('ready','failed'):
                        break
                    time.sleep(3)
                else: raise TimeoutError('Import')
                report['import']={'id':job['id'],'state':job['state'],'risk_state':job.get('risk_state')}
                report['checks']['real_import'] = job['state']=='ready' and job.get('risk_state')=='ready'
            report['passed']=all(report['checks'].values())
        finally:
            if server:
                server.terminate()
                server.wait(timeout=20)
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
    return 0 if report.get('passed') else 1


if __name__=='__main__':
    raise SystemExit(main())
