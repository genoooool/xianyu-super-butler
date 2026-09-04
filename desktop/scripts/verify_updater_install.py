#!/usr/bin/env python3
"""Exercise the actual updater installer on disposable files with signed local assets."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[2]


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--archive',type=Path,required=True)
    args=parser.parse_args(); archive=args.archive.resolve(strict=True)
    root=Path(tempfile.mkdtemp(prefix='xianyu-updater-install-',dir='/tmp')).resolve()
    (root/'DISPOSABLE_UPDATER_FIXTURE').write_text('Isolated test only. No real accounts.\n')
    binary=root/'installed/闲鱼工作台.app/Contents/MacOS/xianyu-workbench'
    binary.parent.mkdir(parents=True); binary.write_bytes(b'previous-test-program')
    data=root/'user-data/qa-and-orders.json'; data.parent.mkdir()
    data.write_text(json.dumps(dict(qa=['test-qa'],orders=['test-order'],images=['test-image'],cookie='fake-disabled-account')))
    conf=json.loads((ROOT/'desktop/src-tauri/tauri.conf.json').read_text())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_): pass
        def do_GET(self):
            if self.path=='/latest.json':
                body=json.dumps(dict(version=conf['version'],data_compatibility=1,
                    url=f'http://127.0.0.1:{self.server.server_port}/app.tar.gz',
                    signature=Path(str(archive)+'.sig').read_text().strip())).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
            elif self.path=='/app.tar.gz':
                self.send_response(200); self.send_header('Content-Length',str(archive.stat().st_size)); self.end_headers()
                with archive.open('rb') as f: shutil.copyfileobj(f,self.wfile)
            else: self.send_error(404)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    (root/'fixture.json').write_text(json.dumps(dict(endpoint=f'http://127.0.0.1:{server.server_port}/latest.json',pubkey=conf['plugins']['updater']['pubkey'])))
    threading.Thread(target=server.serve_forever,daemon=True).start()
    try:
        env=dict(os.environ,XIANYU_UPDATER_TEST_DIR=str(root),TAURI_CONFIG='{"bundle":{"resources":[],"externalBin":[]}}')
        with (root/'test.log').open('w') as log:
            result=subprocess.run(['cargo','test','--bin','xianyu-workbench','real_signed_download_install_and_wrong_signature_rejection','--','--ignored','--nocapture'],
                cwd=ROOT/'desktop/src-tauri',env=env,stdout=log,stderr=log)
        print(json.dumps(dict(test_dir=str(root),exit_code=result.returncode)),flush=True)
        if result.returncode: raise SystemExit(result.returncode)
    finally: server.shutdown(); server.server_close()


if __name__=='__main__': main()
