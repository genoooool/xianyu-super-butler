#!/usr/bin/env python3
"""Built UI against real SQLite control routers; no model or platform requests."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys
import threading
import time

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import expect, sync_playwright
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_human_handoff import Fixture
from app.routers.account_reply_control import create_account_reply_control_router
from app.routers.human_handoff import create_human_handoff_router
from app.services.account_reply_control import AccountReplyControl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--static-dir', type=Path, required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=False)
    fixture = Fixture(); fixture.setUp()
    fixture.service.set_enabled(1, 'a', 'manual', False, 0)
    fixture.begin(chat='human')
    accounts = AccountReplyControl(fixture.db)
    app = FastAPI(); user = lambda: {'user_id':1}
    app.include_router(create_account_reply_control_router(user, fixture.db))
    app.include_router(create_human_handoff_router(user, fixture.db, lambda *_: None))
    delay = {}; saves = []
    @app.middleware('http')
    async def delayed_snapshot(request, call_next):
        held = delay.pop('next', None) if request.url.path == '/chat/reply-control/a' and request.method == 'GET' else None
        response = await call_next(request)
        if held:
            held['started'].set()
            await asyncio.to_thread(held['release'].wait, 20)
        return response
    def get(path, value): app.get(path)(lambda: value)
    get('/verify', dict(authenticated=True, user_id=1, is_admin=False))
    get('/system-settings/public', dict(registration_enabled='false'))
    get('/desktop/notifications/status', dict(available=False, active=False))
    get('/desktop/credentials', dict(available=False, saved=False))
    get('/cookies/details', [dict(id=c, nickname=f'店铺 {c.upper()}', enabled=True) for c in ('a','b')])
    get('/risk-control/status', dict(accounts=[]))
    get('/chat/accounts', dict(data=[dict(accountId=c, displayName=f'店铺 {c.upper()}', connected=True, xianyuUserId='seller') for c in ('a','b')]))
    get('/items', dict(items=[])); get('/message-filters', dict(data=[])); get('/quick-phrases', dict(data=[]))
    def settings(cookie_id):
        return dict(ai_enabled=accounts.state(1, cookie_id)['enabled'], model_name='offline', base_url='https://offline.invalid/v1',
                    api_key='', api_key_configured=True, user_agent='preserve-agent', custom_prompts='',
                    max_discount_percent=10, max_discount_amount=100, max_bargain_rounds=3,
                    context_enabled=True, context_message_limit=12, context_expire_minutes=120)
    app.get('/ai-reply-settings/{cookie_id}')(settings)
    app.get('/ai-reply-settings')(lambda: {c:settings(c) for c in ('a','b')})
    @app.put('/ai-reply-settings/{cookie_id}')
    async def save(cookie_id: str, request: Request):
        data = await request.json(); saves.append((cookie_id, data))
        assert 'ai_enabled' not in data, 'Model save must not override master state'
        assert data['user_agent'] == 'preserve-agent'
        return dict(message='已保存')
    @app.get('/chat/conversations/{cookie_id}')
    def conversations(cookie_id: str):
        return dict(data=dict(conversations=[dict(cid=cid, otherUserId=f'{cookie_id}-{cid}', otherUserName=title,
            lastMessageSummary='离线验证', lastMessageTime=1, unreadCount=0)
            for cid,title in [('ordinary','普通买家'),('manual','人工关闭买家'),('human','待人工买家')]], hasMore=False))
    get('/chat/messages/{cookie_id}/{cid}', dict(data=dict(messages=[], hasMore=False)))
    app.mount('/static', StaticFiles(directory=args.static_dir))
    app.mount('/', StaticFiles(directory=args.static_dir, html=True))
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=lambda:server.run(sockets=[sock]), daemon=True); thread.start()
    try:
        deadline = time.monotonic()+10
        while not server.started and time.monotonic()<deadline: time.sleep(.05)
        assert server.started
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.browser, headless=True)
            page = browser.new_page(viewport=dict(width=1440,height=1000)); errors=[]
            page.on('pageerror', lambda error:errors.append(str(error)))
            page.route('**/*', lambda route:route.continue_() if route.request.url.startswith(base+'/') else route.abort())
            page.add_init_script("""localStorage.setItem('auth_token','offline'); localStorage.setItem('active_page','ai-reply');
                window.__controls=[];const original=window.setInterval;window.setInterval=(fn,ms,...args)=>{
                if(ms===2000)window.__controls.push(fn);return original(fn,ms,...args);};""")
            page.goto(base)
            master = page.get_by_role('switch', name='店铺自动回复总开关')
            expect(master).to_have_attribute('aria-checked','true')
            expect(page.get_by_text('没有 API Key？每天可免费领取额度',exact=True)).to_have_count(0)
            expect(page.get_by_role('link',name='免费领取 token')).to_have_count(0)
            expect(page.get_by_label('接口地址',exact=True)).to_have_value('https://offline.invalid/v1')
            expect(page.get_by_label('模型名称',exact=True)).to_have_value('offline')
            expect(page.get_by_placeholder('输入新密钥以替换',exact=True)).to_be_visible()
            page.emulate_media(color_scheme='dark')
            page.screenshot(path=str(args.output_dir/'ai-model-without-promotion.png'))
            page.emulate_media(color_scheme='light')
            # A delayed pre-toggle GET cannot overwrite the confirmed OFF.
            held = dict(started=threading.Event(), release=threading.Event()); delay['next']=held
            page.evaluate('window.__controls.forEach(fn=>fn())')
            deadline=time.monotonic()+5
            while not held['started'].is_set() and time.monotonic()<deadline: page.wait_for_timeout(50)
            assert held['started'].is_set()
            master.click(); expect(master).to_have_attribute('aria-checked','false')
            held['release'].set(); page.wait_for_timeout(250)
            expect(master).to_have_attribute('aria-checked','false')
            page.get_by_role('button',name='保存配置',exact=True).click()
            expect(page.get_by_text('人工智能回复配置已保存',exact=True)).to_be_visible()
            assert saves and not accounts.state(1,'a')['enabled']
            page.get_by_role('button',name='账号管理',exact=True).click()
            page.get_by_title('AI设置',exact=True).first.click()
            expect(master).to_have_attribute('aria-checked','false')
            master.click(); expect(master).to_have_attribute('aria-checked','true')
            expect(master.locator('[aria-hidden="true"]')).to_have_css('background-color','rgb(67, 136, 100)')
            expect(master.locator('[aria-hidden="true"] > span')).to_have_css('transform','matrix(1, 0, 0, 1, 20, 0)')
            page.screenshot(path=str(args.output_dir/'account-settings-on.png'))
            page.get_by_role('button',name='关闭 AI 助手设置').click()
            page.get_by_role('button',name='AI 回复',exact=True).click()
            expect(master).to_have_attribute('aria-checked','true')
            page.get_by_role('button',name='消息中心',exact=True).click()
            page.get_by_text('普通买家',exact=True).first.click()
            chat = page.get_by_role('switch',name='当前会话 AI 自动回复')
            expect(chat).to_have_attribute('aria-checked','true')
            page.get_by_text('人工关闭买家',exact=True).first.click()
            expect(chat).to_have_attribute('aria-checked','false')
            page.get_by_text('待人工买家',exact=True).first.click()
            expect(chat).to_have_attribute('aria-checked','false')
            page.get_by_text('普通买家',exact=True).first.click()
            expect(chat).to_have_attribute('aria-checked','true')
            # Another window changes the master: the chat poll must follow it.
            accounts.set_enabled(1,'a',False,accounts.state(1,'a')['revision'])
            expect(chat).to_have_attribute('aria-checked','false'); expect(chat).to_be_disabled()
            expect(page.get_by_text('店铺已关闭',exact=True)).to_be_visible()
            expect(page.get_by_placeholder('输入消息',exact=True)).to_be_enabled()
            page.emulate_media(color_scheme='dark')
            page.screenshot(path=str(args.output_dir/'chat-store-off-dark.png'))
            page.get_by_role('button',name='AI 回复',exact=True).click()
            expect(master).to_have_attribute('aria-checked','false')
            page.locator('select:visible').first.select_option('b')
            expect(master).to_have_attribute('aria-checked','true')
            assert not accounts.state(1,'a')['enabled'] and accounts.state(1,'b')['enabled']
            page.set_viewport_size(dict(width=390,height=844))
            page.screenshot(path=str(args.output_dir/'store-b-mobile.png'))
            assert not errors, errors
            report=dict(success=True, checks=['account-settings and AI page share immediate persisted state',
                'stale GET rejected','model save does not change master','ordinary chats follow master',
                'manual/handoff remain off','master off locks chat switch but not composer',
                'other window follows within polling interval','store B isolated'], page_errors=errors)
            (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps(report,ensure_ascii=False)); browser.close()
    finally:
        if 'next' in delay: delay['next']['release'].set()
        server.should_exit=True; thread.join(timeout=5); fixture.tearDown()


if __name__ == '__main__': main()
