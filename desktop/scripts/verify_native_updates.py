#!/usr/bin/env python3
"""Check the real native/Python bridge using a fresh account-free smoke profile."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import psutil
import requests


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--app',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True); args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    binary=(args.app/'Contents/MacOS/xianyu-workbench').resolve(strict=True)
    with (args.output_dir/'native.log').open('w') as log:
        process=subprocess.Popen([str(binary),'--desktop-smoke'],stdout=log,stderr=log)
        owner=psutil.Process(process.pid); children=[]
        try:
            deadline=time.monotonic()+35; runtime=None
            while time.monotonic()<deadline:
                if process.poll() is not None: raise RuntimeError('Native launcher exited')
                children=owner.children(recursive=True)
                for child in children:
                    try: env=child.environ()
                    except psutil.NoSuchProcess: continue
                    if env.get('XIANYU_DESKTOP_SMOKE')=='1' and env.get('XIANYU_DESKTOP_TOKEN'):
                        runtime=Path(env['XIANYU_DATA_DIR']); token=env['XIANYU_DESKTOP_TOKEN']; port=env['API_PORT']; break
                if runtime: break
                time.sleep(.15)
            if runtime is None: raise RuntimeError('Could not resolve this smoke backend')
            assert runtime.name.startswith('xianyu-desktop-smoke-') and 'Application Support' not in str(runtime)
            base=f'http://127.0.0.1:{port}'; session=requests.Session(); session.trust_env=False
            while time.monotonic()<deadline:
                try:
                    if session.get(base+'/health',timeout=.5).ok: break
                except requests.RequestException: pass
                time.sleep(.2)
            else: raise RuntimeError('Readiness timeout')
            session.get(base+'/desktop/bootstrap',params={'token':token}).raise_for_status()
            login=session.post(base+'/login',json={'username':'admin','password':'admin123'}).json()
            assert login.get('success'); session.headers['Authorization']='Bearer '+login['token']
            status=session.get(base+'/desktop/updates/status'); status.raise_for_status()
            assert status.json()['version']=='1.0.0' and status.json()['available']
            assert session.get(base+'/desktop/updates/poll').status_code==403
            assert session.post(base+'/desktop/updates/action',json={'action':'install','version':'9.9.9'}).status_code==409
            session.post(base+'/desktop/updates/action',json={'action':'check','version':''}).raise_for_status()
            deadline=time.monotonic()+65
            while time.monotonic()<deadline:
                status=session.get(base+'/desktop/updates/status').json()
                if status['phase'] not in ('idle','checking'): break
                time.sleep(.3)
            else: raise RuntimeError('Native bridge did not answer')
            assert status['phase'] in ('unpublished','error','current','available','incompatible'),status
            assert status['version']=='1.0.0'
            # The legacy desktop announcement endpoint must remain inert.
            old=session.get(base+'/api/announcement',params={'force':True}).json()
            assert not old['source_configured'] and not old['announcements']
            assert session.get(base+'/cookies/details').json()==[]
            report=dict(status='passed',phase=status['phase'],runtime=str(runtime),version=status['version'],
                platform_messages=0,installed=False,legacy_source_disabled=True)
            (args.output_dir/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps(report,ensure_ascii=False),flush=True)
        finally:
            children=owner.children(recursive=True) if process.poll() is None else children
            if process.poll() is None:
                process.terminate(); process.wait(timeout=12)
            _,alive=psutil.wait_procs(children,timeout=12)
            assert not [p for p in alive if p.is_running() and p.status()!=psutil.STATUS_ZOMBIE], 'Smoke children remain'


if __name__=='__main__': main()
