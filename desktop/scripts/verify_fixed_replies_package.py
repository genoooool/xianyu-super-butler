#!/usr/bin/env python3
"""Exercise the signed frozen backend against a fresh, account-free data directory."""
import argparse
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import time

from PIL import Image
import psutil
import requests


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--backend',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    token=secrets.token_urlsafe(32)
    env=dict(os.environ,XIANYU_DESKTOP='1',XIANYU_DESKTOP_SMOKE='1',XIANYU_DATA_DIR=str(args.output_dir),
             XIANYU_DESKTOP_TOKEN=token,API_HOST='127.0.0.1',API_PORT=str(port),SQL_LOG_ENABLED='false')
    env.pop('DB_PATH',None)
    base=f'http://127.0.0.1:{port}'
    session=requests.Session(); session.trust_env=False
    with (args.output_dir/'backend.log').open('w') as log:
        started=time.monotonic()
        process=subprocess.Popen([str(args.backend)],cwd=args.output_dir,env=env,stdout=log,stderr=log)
        owner=psutil.Process(process.pid); descendants=[]
        try:
            while time.monotonic()-started<45:
                if process.poll() is not None: raise RuntimeError('backend exited')
                try:
                    if session.get(base+'/health',timeout=.4).ok: break
                except requests.RequestException: pass
                time.sleep(.1)
            else: raise RuntimeError('readiness timeout')
            ready=round(time.monotonic()-started,3)
            assert session.get(base+'/ai-knowledge').status_code==403
            session.get(base+'/desktop/bootstrap',params=dict(token=token)).raise_for_status()
            assert session.get(base+'/ai-knowledge').status_code==401
            login=session.post(base+'/login',json=dict(username='admin',password='admin123')).json()
            assert login['success']; session.headers['Authorization']='Bearer '+login['token']
            def post(path,**kw):
                response=session.post(base+path,timeout=10,**kw); response.raise_for_status(); return response.json()
            raw=io.BytesIO(); Image.new('RGB',(18,12),'gold').save(raw,format='PNG')
            asset=post('/ai-knowledge/images',data=raw.getvalue(),headers={'Content-Type':'application/octet-stream'})
            entry=post('/ai-knowledge',json=dict(scope='shared',topic='离线报价图',entry_type='qa',image_ids=[asset['id']]))['entry']
            assert entry['content']=='' and entry['entry_type']=='qa'
            image=session.get(base+'/ai-knowledge/images/'+asset['id']); image.raise_for_status()
            assert image.content.startswith(b'\x89PNG') and image.headers['Cache-Control']=='no-store'
            phrase=post('/quick-phrases',data=dict(title='离线图片短语',content='',image_ids=json.dumps([asset['id']])))
            phrases=session.get(base+'/quick-phrases').json()['data']
            assert len(phrases)==1 and phrases[0]['image_ids']==[asset['id']]
            response=session.put(base+'/quick-phrases/'+str(phrases[0]['id']),data=dict(enabled='false')); response.raise_for_status()
            assert session.get(base+'/quick-phrases').json()['data']==[]
            assert len(session.get(base+'/quick-phrases',params=dict(include_disabled='true')).json()['data'])==1
            backup=session.get(base+'/backup/export'); backup.raise_for_status(); document=backup.json()
            assert document['data']['reply_assets'] and document['data']['chat_quick_phrases']
            response=post('/backup/import',files={'file':('offline-backup.json',backup.content,'application/json')})
            assert '成功' in response.get('message','')
            restored=session.get(base+'/ai-knowledge').json()['entries']
            assert len(restored)==1 and restored[0]['image_ids']==[asset['id']]
            assert session.get(base+'/ai-knowledge/images/'+asset['id']).content==image.content
            assert session.post(base+'/chat/send/not-owned',json=dict(cid='test',to_user_id='nobody',image_ids=[asset['id']])).status_code==403
            assert session.get(base+'/chat/handoffs').json()['entries']==[]
            # This is the isolated smoke DB, never a production account. Seed local-only state AFTER backup tests.
            db=sqlite3.connect(args.output_dir/'data/xianyu_data.db')
            owner_id=db.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
            db.execute("INSERT INTO cookies(id,value,user_id) VALUES('offline-handoff','not-a-platform-cookie',?)",(owner_id,))
            db.execute("INSERT INTO cookie_status(cookie_id,enabled) VALUES('offline-handoff',0)")
            db.execute("INSERT INTO ai_reply_settings(cookie_id,ai_enabled) VALUES('offline-handoff',0)")
            db.execute("INSERT INTO item_info(cookie_id,item_id,item_title) VALUES('offline-handoff','one','离线商品一'),('offline-handoff','two','离线商品二')")
            db.execute("""INSERT INTO chat_human_handoffs
                (owner_id,cookie_id,chat_id,revision,pending,reason,buyer_id,buyer_name,item_id,created_ms,send_status)
                VALUES(?,'offline-handoff','offline-chat',1,1,'unclear','nobody','离线测试','',1,'unknown')""",(owner_id,))
            db.commit()
            # Real frozen CRUD API, still on a disabled synthetic account.
            grouped=post('/ai-knowledge/qa',json=dict(scope='item',cookie_id='offline-handoff',
                item_ids=['one','two'],topic='多商品问候',keywords='你好',image_ids=[asset['id']]))['entry']
            assert grouped['item_ids']==['one','two'] and grouped['content']==''
            route=base+'/ai-knowledge/qa/'+str(grouped['id'])
            update=session.put(route,json={**grouped,'topic':'改名问候','scope':'account','item_id':'','item_ids':[]})
            update.raise_for_status(); changed=update.json()['entry']
            assert changed['id']==grouped['id'] and changed['scope']=='account' and changed['item_ids']==[]
            assert session.put(route,json=grouped).status_code==409
            assert session.delete(route,params={'revision':grouped['revision']}).status_code==409
            session.delete(route,params={'revision':changed['revision']}).raise_for_status()
            assert session.put(route,json=changed).status_code==404
            assert session.get(base+'/ai-knowledge/images/'+asset['id']).content==image.content
            entries=session.get(base+'/chat/handoffs').json()['entries']
            assert len(entries)==1 and entries[0]['send_status']=='unknown'
            # Frozen manual-reply import/route and offline rejection: never clear a takeover without delivery.
            unavailable=session.post(base+'/chat/send/offline-handoff',json=dict(
                cid='offline-chat',to_user_id='nobody',text='离线隔离验证'))
            assert unavailable.status_code in (409,503), unavailable.text
            assert len(session.get(base+'/chat/handoffs').json()['entries'])==1
            endpoint='/chat/handoffs/offline-handoff/offline-chat/resume'
            assert session.post(base+endpoint,json={'revision':2}).status_code==409
            post(endpoint,json={'revision':1})
            assert session.post(base+endpoint,json={'revision':1}).status_code==409
            assert session.get(base+'/chat/handoffs').json()['entries']==[]
            assert db.execute("SELECT ai_enabled FROM ai_reply_settings WHERE cookie_id='offline-handoff'").fetchone()[0]==0
            assert db.execute("SELECT pending,revision FROM chat_human_handoffs").fetchone()==(0,2)
            assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            db.close()
            report=dict(status='passed',ready_seconds=ready,packaged_qa_image=True,phrase_image=True,backup_roundtrip=True,
                        private_asset_guard=True,handoff_resume=True,multiselect_crud=True,offline_send_keeps_handoff=True,
                        ai_enablement_unchanged=True,buyer_messages_sent=0)
            (args.output_dir/'result.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps(report),flush=True)
        finally:
            if process.poll() is None:
                descendants=owner.children(recursive=True); process.terminate()
                try: process.wait(timeout=12)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            _,alive=psutil.wait_procs(descendants,timeout=5)
            for child in alive: child.kill()
            if alive: raise RuntimeError('test backend left live descendants')


if __name__=='__main__': main()
