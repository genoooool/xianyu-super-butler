#!/usr/bin/env python3
"""Isolated built UI verification: SQLite image/QA/phrase persistence, no model/buyer traffic."""
import argparse
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from PIL import Image
from playwright.sync_api import expect, sync_playwright
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.routers.ai_knowledge import create_ai_knowledge_router
from app.services.ai_knowledge import KnowledgeService, initialize_schema
from app.services.quick_phrases import QuickPhrases, initialize_schema as phrases_schema


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--static-dir',required=True,type=Path)
    parser.add_argument('--browser',required=True)
    parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    db=SimpleNamespace(conn=sqlite3.connect(args.output_dir/'ui.sqlite3',check_same_thread=False),lock=threading.RLock())
    db.conn.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY); INSERT INTO users VALUES(1);
        CREATE TABLE cookies(id TEXT PRIMARY KEY,user_id INTEGER); INSERT INTO cookies VALUES('a',1),('b',1);
        CREATE TABLE item_info(cookie_id TEXT,item_id TEXT); INSERT INTO item_info VALUES('a','one');
        CREATE TABLE chat_quick_phrases(id INTEGER PRIMARY KEY,category TEXT DEFAULT '默认',title TEXT,content TEXT,
        sort_order INTEGER DEFAULT 0,enabled INTEGER DEFAULT 1,use_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);''')
    initialize_schema(db.conn.cursor()); phrases_schema(db.conn.cursor()); db.conn.commit()
    phrases=QuickPhrases(db)
    def user(authorization: str=Header(default='')):
        if authorization!='Bearer offline-test': raise HTTPException(401)
        return {'user_id':1}
    app=FastAPI(); app.include_router(create_ai_knowledge_router(user,db))
    def add_get(path,value): app.get(path)(lambda: value)
    add_get('/verify',dict(authenticated=True,user_id=1,is_admin=False))
    add_get('/system-settings/public',dict(registration_enabled='false'))
    add_get('/desktop/notifications/status',dict(available=False,active=False))
    add_get('/desktop/credentials',dict(available=False,saved=False))
    add_get('/cookies/details',[dict(id='a',nickname='测试店铺 A',enabled=False),dict(id='b',nickname='测试店铺 B',enabled=False)])
    add_get('/items',dict(items=[dict(cookie_id='a',item_id='one',item_title='测试商品：星星套餐')]))
    add_get('/default-replies/{cookie_id}',dict(enabled=False))
    add_get('/keywords/{cookie_id}',[])
    add_get('/reply-rules/{cookie_id}',[])
    add_get('/keywords-with-item-id/{cookie_id}',[])
    add_get('/system-settings',{})
    add_get('/message-filters',dict(data=[]))
    add_get('/chat/accounts',dict(data=[dict(accountId='a',displayName='测试店铺 A',connected=True)]))
    add_get('/chat/conversations/{cookie_id}',dict(data=dict(conversations=[dict(cid='chat',otherUserId='buyer',otherUserName='离线测试买家',lastMessageSummary='想看报价图',lastMessageTime=1,unreadCount=1,itemId='one')],hasMore=False)))
    add_get('/chat/messages/{cookie_id}/{cid}',dict(data=dict(messages=[],hasMore=False)))
    @app.get('/quick-phrases')
    def list_phrases(include_disabled: bool=False): return dict(success=True,data=phrases.list(1,include_disabled))
    @app.post('/quick-phrases')
    async def create_phrase(request: Request):
        form=dict(await request.form()); return dict(success=True,id=phrases.save(1,form))
    @app.post('/quick-phrases/{phrase_id}/use')
    def use_phrase(phrase_id: int): return dict(success=phrases.use(1,phrase_id))
    sends=[]
    @app.post('/chat/send/{cookie_id}')
    async def record_send(request: Request):
        sends.append(await request.json()); return dict(success=True,message='仅记录离线请求')
    app.mount('/static',StaticFiles(directory=args.static_dir))
    app.mount('/',StaticFiles(directory=args.static_dir,html=True))
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); base=f'http://127.0.0.1:{sock.getsockname()[1]}'
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=lambda: server.run(sockets=[sock]),daemon=True); thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline: time.sleep(.05)
        assert server.started
        output=io.BytesIO(); Image.new('RGB',(240,160),'gold').save(output,format='PNG')
        upload=dict(name='报价.png',mimeType='image/png',buffer=output.getvalue())
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=args.browser,headless=True)
            context=browser.new_context(viewport=dict(width=1440,height=1000))
            page=context.new_page(); errors=[]; failed=[]
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.on('response',lambda response: failed.append(response.url) if response.status>=400 else None)
            page.route('**/*',lambda route: route.continue_() if route.request.url.startswith(base+'/') else route.abort())
            page.add_init_script("localStorage.setItem('auth_token','offline-test'); localStorage.setItem('active_page','auto-reply')")
            page.goto(base)
            page.get_by_role('button',name='添加意图回复',exact=True).click()
            modal=page.get_by_role('dialog',name='添加意图回复')
            expect(modal).to_be_visible()
            modal.get_by_label('回复范围',exact=True).select_option('item')
            modal.get_by_label('回复所属商品',exact=True).select_option('one')
            modal.get_by_label('QA问题意图').fill('客户询价')
            modal.get_by_label('QA常见问法').fill('多少钱，怎么卖')
            modal.get_by_label('添加回复图片').set_input_files(upload)
            expect(modal.get_by_alt_text('回复图片')).to_be_visible()
            modal.screenshot(path=str(args.output_dir/'intent-modal-light.png'))
            page.emulate_media(color_scheme='dark')
            expect(page.locator('html')).to_have_attribute('data-theme','dark')
            expect(modal.get_by_label('QA问题意图')).to_have_css('background-color','rgb(25, 25, 22)')
            page.screenshot(path=str(args.output_dir/'intent-modal-dark.png'), animations='disabled')
            modal.get_by_role('button',name='保存固定回复').click()
            expect(modal).to_have_count(0)
            row=page.locator('article').filter(has_text='客户询价')
            expect(row.get_by_alt_text('回复图片')).to_be_visible()
            expect(row).to_contain_text('商品专属')
            page.get_by_role('button',name='停用 客户询价',exact=True).click()
            expect(row).to_contain_text('已停用')
            assert 'bg-red-100' in row.get_by_text('已停用',exact=True).get_attribute('class')
            page.get_by_role('button',name='启用 客户询价',exact=True).click()
            expect(row).to_contain_text('已启用')
            assert 'bg-green-100' in row.get_by_text('已启用',exact=True).get_attribute('class')
            page.reload(); expect(row).to_contain_text('客户询价'); expect(row.get_by_alt_text('回复图片')).to_be_visible()
            page.get_by_role('button',name='编辑 客户询价',exact=True).click()
            modal=page.get_by_role('dialog',name='编辑固定回复')
            expect(modal.get_by_label('QA固定答案')).to_have_value('')
            modal.get_by_label('QA固定答案').fill('点开商品购买页面查看对应价格哦亲。')
            modal.get_by_role('button',name='保存固定回复').click()
            expect(row).to_contain_text('点开商品购买页面')
            page.get_by_role('tab',name='关键词回复').click()
            page.get_by_role('button',name='添加关键词回复',exact=True).click()
            modal=page.get_by_role('dialog',name='添加关键词回复')
            modal.get_by_label('QA问题意图').fill('你好')
            modal.get_by_label('添加回复图片').set_input_files(upload)
            expect(modal.get_by_alt_text('回复图片')).to_be_visible()
            modal.get_by_role('button',name='保存固定回复').click()
            expect(page.locator('article').filter(has_text='你好')).to_be_visible()
            page.get_by_role('button',name='添加关键词回复',exact=True).click()
            page.set_viewport_size(dict(width=390,height=844))
            expect(page.get_by_role('dialog')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(args.output_dir/'keyword-modal-mobile.png'))
            page.get_by_role('button',name='取消',exact=True).click()
            page.set_viewport_size(dict(width=1440,height=1000))
            # Existing system-settings page: image-only phrase persists in the real service.
            page.get_by_role('button',name='系统设置',exact=True).click()
            page.get_by_role('tab',name='快捷短语').click()
            page.get_by_placeholder('标题',exact=True).fill('价格图片')
            page.get_by_label('添加回复图片').set_input_files(upload)
            expect(page.locator('img[alt="回复图片"]:visible').first).to_be_visible()
            page.get_by_role('button',name='添加',exact=True).click()
            expect(page.get_by_text('价格图片',exact=True)).to_be_visible()
            assert phrases.list(1)[0]['content']=='' and phrases.list(1)[0]['image_ids']
            page.get_by_role('button',name='消息中心',exact=True).click()
            page.get_by_text('离线测试买家',exact=True).first.click()
            page.get_by_title('快捷短语',exact=True).click()
            page.get_by_role('button',name='[默认] 价格图片').click()
            expect(page.locator('img[alt="回复图片"]:visible')).to_be_visible()
            page.screenshot(path=str(args.output_dir/'message-image-draft.png'))
            page.get_by_role('button',name='发送',exact=True).click()
            expect(page.locator('img[alt="回复图片"]:visible')).to_have_count(0)
            assert len(sends)==1 and sends[0]['text']=='' and len(sends[0]['image_ids'])==1
            assert not errors, errors
            assert not failed, failed
            print(json.dumps(dict(status='passed',qa_entries=len(KnowledgeService(db).list_entries(1)),phrases=len(phrases.list(1)),offline_send_requests=len(sends),console_errors=errors,http_errors=failed),ensure_ascii=False))
            browser.close()
    finally:
        server.should_exit=True; thread.join(timeout=10); db.conn.close()


if __name__=='__main__': main()
