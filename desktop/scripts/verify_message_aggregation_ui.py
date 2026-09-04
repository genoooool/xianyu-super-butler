#!/usr/bin/env python3
"""Built UI: mixed shops stay scoped by account; no platform/model traffic."""
import argparse
import json
from pathlib import Path
import socket
import threading
import time

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import expect, sync_playwright
import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--static-dir', type=Path, required=True)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=False)
    app = FastAPI(); sent = []; activation = {'value': None}
    def get(path, value): app.get(path)(lambda: value)
    get('/verify', dict(authenticated=True, user_id=1, is_admin=False))
    get('/system-settings/public', dict(registration_enabled='false'))
    get('/desktop/notifications/status', dict(available=True, active=True, sound_available=False,
        preference=dict(enabled=True, sound=False)))
    get('/desktop/credentials', dict(available=False, saved=False))
    get('/cookies/details', [dict(id=c, nickname=f'店铺 {c.upper()}', enabled=True) for c in ('a','b')])
    get('/risk-control/status', dict(accounts=[]))
    get('/chat/accounts', dict(data=[dict(accountId=c, displayName=f'店铺 {c.upper()}', connected=True,
        xianyuUserId=f'seller-{c}') for c in ('a','b')]))
    get('/items', dict(items=[])); get('/message-filters', dict(data=[])); get('/quick-phrases', dict(data=[]))
    get('/chat/handoffs', dict(entries=[]))
    @app.post('/desktop/notifications/session')
    def notification_session():
        return dict(success=True)
    @app.post('/desktop/notifications/activation')
    def notification_activation():
        value = activation['value']
        activation['value'] = None
        return dict(navigation=value)
    @app.get('/chat/handoffs/{account}/{cid}')
    def control(account: str, cid: str):
        return dict(cookie_id=account, chat_id=cid, enabled=True, conversation_enabled=True,
                    revision=0, reason='', account_enabled=True, account_revision=0)
    @app.get('/chat/conversations/{account}')
    def conversations(account: str):
        if account == 'a':
            rows = [dict(cid='shared', rawCid='shared@goofish', otherUserId='buyer-a', otherUserName='甲店买家',
                         lastMessageSummary='甲店消息', lastMessageTime=200, unreadCount=1),
                    dict(cid='legacy', rawCid='legacy@goofish', otherUserId='-1', otherUserName='',
                         lastMessageSummary='历史真实会话', lastMessageTime=100, unreadCount=0)]
        else:
            rows = [dict(cid='shared', rawCid='shared@goofish', otherUserId='buyer-b', otherUserName='',
                         lastMessageSummary='乙店消息', lastMessageTime=300, unreadCount=2)]
        return dict(data=dict(conversations=rows, hasMore=False))
    @app.get('/chat/messages/{account}/{cid}')
    def messages(account: str, cid: str):
        name = '乙店真实昵称' if account == 'b' else '甲店买家'
        buyer = 'buyer-b' if account == 'b' else '-1' if cid == 'legacy' else 'buyer-a'
        return dict(data=dict(messages=[dict(messageId=f'{account}-{cid}', senderId=buyer, senderName=name,
            isSelf=False, type='text', text=f'{account}-{cid}-history', images=[], time=10)], hasMore=False))
    @app.post('/chat/send/{account}')
    async def send(account: str, request: Request):
        payload = await request.json(); sent.append(dict(account=account, **payload))
        return dict(success=True, data=dict(status='sent', messageIds=[f'm-{len(sent)}']))
    app.mount('/static', StaticFiles(directory=args.static_dir))
    app.mount('/', StaticFiles(directory=args.static_dir, html=True))
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); base=f'http://127.0.0.1:{sock.getsockname()[1]}'
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not server.started and time.monotonic()<deadline: time.sleep(.05)
        assert server.started
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=args.browser,headless=True)
            page=browser.new_page(viewport=dict(width=1440,height=950));errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base+'/') else route.abort())
            page.add_init_script("localStorage.setItem('auth_token','offline');localStorage.setItem('active_page','messages')")
            page.goto(base)
            selector=page.get_by_label('消息账号')
            expect(selector).to_have_value('__all__')
            expect(page.get_by_text('甲店买家',exact=True).first).to_be_visible()
            expect(page.get_by_text('闲鱼用户 -1',exact=True).first).to_be_visible()
            # The newest conversation is selected on desktop; its explicit
            # history nickname must immediately replace the numeric fallback.
            expect(page.get_by_text('乙店真实昵称',exact=True).first).to_be_visible()
            expect(page.get_by_text('店铺 A',exact=True).first).to_be_visible()
            expect(page.get_by_text('店铺 B',exact=True).first).to_be_visible()
            b_row=page.locator('[data-account-id="b"][data-conversation-id="shared"]')
            a_row=page.locator('[data-account-id="a"][data-conversation-id="shared"]')
            b_row.click()
            expect(selector).to_have_value('__all__')
            expect(a_row).to_be_visible()
            expect(page.get_by_text('乙店真实昵称',exact=True).first).to_be_visible()
            expect(page.get_by_text('b-shared-history',exact=True)).to_be_visible()
            draft=page.get_by_placeholder('输入消息',exact=True);draft.fill('必须由乙店发送')
            page.get_by_role('button',name='发送',exact=True).click()
            expect(page.get_by_text('已发送',exact=True)).to_be_visible()
            assert sent == [dict(account='b',cid='shared',to_user_id='buyer-b',text='必须由乙店发送',image_ids=[])]
            selector.select_option('a')
            expect(page.get_by_text('乙店真实昵称',exact=True)).to_have_count(0)
            expect(page.get_by_text('甲店买家',exact=True).first).to_be_visible()
            selector.select_option('__all__')
            expect(page.get_by_text('乙店真实昵称',exact=True).first).to_be_visible()
            # Notification activation opens the exact owning shop/chat while
            # keeping the left list in aggregate mode.
            activation['value'] = dict(id='notice-a', account_id='a', chat_id='outside-page',
                buyer_id='outside-a', buyer_name='通知买家甲')
            expect(page.get_by_text('a-outside-page-history',exact=True)).to_be_visible(timeout=5000)
            expect(page.get_by_role('heading',name='通知买家甲',exact=True)).to_be_visible()
            expect(selector).to_have_value('__all__')
            expect(b_row).to_be_visible()
            page.emulate_media(color_scheme='dark');page.screenshot(path=str(args.output_dir/'mixed-dark.png'))
            page.set_viewport_size(dict(width=390,height=844));page.screenshot(path=str(args.output_dir/'mixed-mobile.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors,errors
            report=dict(success=True,checks=['all accounts default','mixed list remains after opening chat',
                'same cid isolated by account','send uses owning account','single-shop filter retained',
                'history nickname updates immediately','legacy -1 conversation preserved',
                'notification opens exact chat and keeps aggregate list','responsive layout'],
                sent_messages=1,platform_requests=0,model_requests=0,page_errors=errors)
            (args.output_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(json.dumps(report,ensure_ascii=False));browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=5)

if __name__=='__main__': main()
