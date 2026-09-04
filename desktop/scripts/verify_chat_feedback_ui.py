#!/usr/bin/env python3
"""Built React + actual manual delivery service; fake platform, no buyer/model traffic."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import expect, sync_playwright
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_manual_reply_resume import Fixture
from app.routers.human_handoff import create_human_handoff_router
from app.routers.ai_knowledge import create_ai_knowledge_router
from app.services.ai_knowledge import initialize_schema
from app.services.manual_reply import send_manual_reply_outcome


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--static-dir', required=True, type=Path)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=False)
    fixture = Fixture(); fixture.setUp()
    fixture.db.conn.execute('CREATE TABLE item_info(cookie_id TEXT,item_id TEXT)')
    initialize_schema(fixture.db.conn.cursor()); fixture.db.conn.commit()
    user = lambda: dict(user_id=1)
    app = FastAPI()
    app.include_router(create_human_handoff_router(user, fixture.db, lambda *_: None))
    app.include_router(create_ai_knowledge_router(user, fixture.db))
    def get(path, value): app.get(path)(lambda: value)
    get('/verify', dict(authenticated=True, user_id=1, is_admin=False))
    get('/system-settings/public', dict(registration_enabled='false'))
    get('/desktop/notifications/status', dict(available=False, active=False))
    get('/desktop/credentials', dict(available=False, saved=False))
    get('/cookies/details', [dict(id=c, nickname=f'店铺 {c.upper()}', enabled=False) for c in ('a', 'b')])
    get('/items', dict(items=[])); get('/message-filters', dict(data=[]))
    get('/chat/accounts', dict(data=[dict(accountId=c, displayName=f'店铺 {c.upper()}', connected=True, xianyuUserId='seller') for c in ('a', 'b')]))
    get('/quick-phrases', dict(data=[dict(id=1, title='测试图片', category='默认', content='', image_ids=[fixture.image])]))
    app.post('/quick-phrases/{phrase_id}/use')(lambda: dict(success=True))
    @app.get('/chat/conversations/{cookie_id}')
    def conversations(cookie_id: str):
        return dict(data=dict(conversations=[dict(cid=cid, otherUserId=f'{cookie_id}-{cid}-buyer',
            otherUserName=f'{cookie_id.upper()}店买家{index}', lastMessageSummary='离线测试', lastMessageTime=1, unreadCount=0)
            for index, cid in enumerate(('chat', 'second'), 1)], hasMore=False))

    records = []; history = []; release = threading.Event()
    state = dict(mode='hold', visible=False, stale=None)
    @app.get('/test-state')
    def test_state(): return dict(stale_started=state.get('stale_started', False))
    @app.get('/chat/messages/{cookie_id}/{cid}')
    async def messages(cookie_id: str, cid: str):
        stale = state.pop('stale', None)
        if stale:
            stale['started'].set()
            state['stale_started'] = True
            await asyncio.to_thread(stale['release'].wait, 20)
            return dict(data=dict(messages=[], hasMore=False))
        rows = [m for m in history if m['account'] == cookie_id and m['cid'] == cid] if state['visible'] else []
        return dict(data=dict(messages=rows, hasMore=False))

    @app.post('/chat/send/{cookie_id}')
    async def send(cookie_id: str, request: Request):
        payload = await request.json(); records.append(dict(account=cookie_id, **payload))
        seq = len(records); mode = state['mode']
        instance = fixture.instance; instance.cookie_id = cookie_id
        async def receipt(part):
            if mode == 'hold':
                assert await asyncio.to_thread(release.wait, 20), 'UI test did not release receipt'
            if mode == 'reject' or mode == 'partial' and part == 'image':
                return dict(code=403, body={})
            if mode == 'timeout': raise TimeoutError('offline')
            if mode == 'conflict': return dict(code=200, body=dict(code=403))
            message_id = f'server-{seq}-{part}'
            history.append(dict(account=cookie_id, cid=payload['cid'], messageId=message_id,
                senderId='seller', senderName='卖家', isSelf=True, type='text', images=[],
                text=payload['text'] if part == 'text' else '[离线图片]', time=int(time.time()*1000)))
            return dict(code=200, body=dict(messageId=message_id))
        async def text(*_): return await receipt('text')
        async def image(*_): return await receipt('image')
        instance.send_im_text.side_effect = text; instance._send_im_request.side_effect = image
        result = await send_manual_reply_outcome(instance, fixture.db, 1, payload['cid'], payload['to_user_id'],
                                                  payload['text'], payload.get('image_ids', []), lambda *_: None)
        if mode == 'http502': raise HTTPException(502, 'offline gateway lost confirmed response')
        return result

    app.mount('/static', StaticFiles(directory=args.static_dir))
    app.mount('/', StaticFiles(directory=args.static_dir, html=True))
    sock = socket.socket(); sock.bind(('127.0.0.1', 0)); base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline: time.sleep(.05)
        assert server.started
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.browser, headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1000)); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base+'/') else route.abort())
            page.add_init_script("""localStorage.setItem('auth_token','offline-test'); localStorage.setItem('active_page','messages');
                window.__polls=[]; const original=window.setInterval; window.setInterval=(fn,ms,...args)=>{
                if(ms===10000)window.__polls.push(fn); return original(fn,ms,...args); };""")
            page.goto(base); page.get_by_text('A店买家1', exact=True).first.click()
            composer = page.locator('footer:visible'); draft = composer.get_by_placeholder('输入消息', exact=True)
            def submit(text, mode):
                state['mode'] = mode; draft.fill(text); composer.get_by_role('button', name='发送', exact=True).click()
                return page.locator('[data-send-status]').filter(has=page.get_by_text(text, exact=True)).last
            def poll(): page.evaluate('window.__polls.forEach(fn=>fn())')
            bubble = submit('这条消息应立即显示', 'hold')
            expect(bubble).to_have_attribute('data-send-status', 'sending')
            expect(bubble.get_by_text('发送中…', exact=True)).to_be_visible()
            expect(draft).to_have_value('')
            page.screenshot(path=str(args.output_dir/'sending-light.png'))
            release.set(); expect(bubble).to_have_attribute('data-send-status', 'sent')
            expect(bubble.get_by_text('已发送', exact=True)).to_be_visible()
            assert bubble.get_by_role('status').bounding_box()['y'] > bubble.get_by_text('这条消息应立即显示', exact=True).bounding_box()['y']

            # History arriving late must not duplicate the confirmed optimistic message.
            state['visible'] = True; poll()
            expect(page.get_by_text('这条消息应立即显示', exact=True)).to_have_count(1)
            stale = dict(started=threading.Event(), release=threading.Event()); state['stale'] = stale
            poll()
            page.wait_for_function("async () => (await (await fetch('/test-state')).json()).stale_started")
            history.append(dict(account='a', cid='chat', messageId='fresh-incoming', senderId='buyer', senderName='买家',
                                isSelf=False, type='text', text='新的聊天记录不能消失', images=[], time=int(time.time()*1000)))
            next_bubble = submit('触发新一轮同步', 'success')
            expect(next_bubble).to_have_attribute('data-send-status', 'sent')
            expect(page.get_by_text('新的聊天记录不能消失', exact=True)).to_be_visible()
            stale['release'].set()

            failed = submit('平台拒绝的原文', 'reject')
            expect(failed).to_have_attribute('data-send-status', 'failed')
            red = failed.get_by_role('button', name='发送失败，点击重新发送')
            assert red.bounding_box()['x'] < failed.get_by_text('平台拒绝的原文', exact=True).bounding_box()['x']
            before = len(records); red.click()
            modal = page.get_by_role('alertdialog', name='重新发送消息')
            expect(modal).to_be_visible(); expect(modal).to_contain_text('A店买家1')
            modal.get_by_role('button', name='取消', exact=True).click(); assert len(records) == before
            draft.fill('新的草稿不要覆盖原文'); red.click(); state['mode'] = 'success'
            modal.get_by_role('button', name='重新发送', exact=True).dblclick()
            expect(failed).to_have_attribute('data-send-status', 'sent')
            assert len(records) == before+1 and records[-1]['text'] == '平台拒绝的原文'
            expect(draft).to_have_value('新的草稿不要覆盖原文')
            expect(page.get_by_text('新的聊天记录不能消失', exact=True)).to_be_visible()

            failed = submit('点红叹号可以确认重发', 'reject')
            expect(failed).to_have_attribute('data-send-status', 'failed')
            page.emulate_media(color_scheme='dark'); page.screenshot(path=str(args.output_dir/'failed-dark.png'))
            failed.get_by_role('button', name='发送失败，点击重新发送').click()
            page.screenshot(path=str(args.output_dir/'retry-confirm-dark.png'))
            page.get_by_role('alertdialog').get_by_role('button', name='取消', exact=True).click()
            page.set_viewport_size(dict(width=390, height=844))
            failed.get_by_role('button', name='发送失败，点击重新发送').click(trial=True)
            page.screenshot(path=str(args.output_dir/'failed-mobile.png'), animations='disabled')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.set_viewport_size(dict(width=1440, height=1000))

            for mode in ('timeout', 'http502', 'conflict'):
                unknown = submit(f'未知结果-{mode}', mode)
                expect(unknown).to_have_attribute('data-send-status', 'unconfirmed')
                expect(unknown.get_by_role('button')).to_have_count(0)
            # Text succeeds but image is rejected: never offer whole-message retry.
            page.get_by_title('快捷短语', exact=True).click()
            page.get_by_role('button', name='[默认] 测试图片').click()
            partial = submit('文字成功图片失败', 'partial')
            expect(partial).to_have_attribute('data-send-status', 'unconfirmed')
            expect(partial.get_by_alt_text('回复图片')).to_be_visible()
            expect(partial.get_by_role('button')).to_have_count(0)
            poll()
            expect(page.get_by_text('文字成功图片失败', exact=True)).to_have_count(1)
            expect(composer.get_by_label('添加回复图片')).to_have_count(0)
            page.screenshot(path=str(args.output_dir/'unconfirmed-dark.png'))
            count = len(records)
            page.get_by_text('A店买家2', exact=True).first.click()
            expect(page.locator('[data-send-status]')).to_have_count(0)
            page.get_by_text('A店买家1', exact=True).first.click()
            expect(partial).to_be_visible()
            page.locator('select').filter(has=page.locator('option[value="b"]')).first.select_option('b')
            page.get_by_text('B店买家1', exact=True).first.click()
            expect(page.locator('[data-send-status]')).to_have_count(0)
            assert len(records) == count, 'No automatic resend on switching or polling'
            assert not errors, errors
            report = dict(status='passed', offline_http_sends=len(records), live_buyer_sends=0, console_errors=errors)
            (args.output_dir/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False)); browser.close()
    finally:
        release.set()
        if state.get('stale'): state['stale']['release'].set()
        server.should_exit = True; thread.join(timeout=25); fixture.tearDown()
        assert not thread.is_alive()


if __name__ == '__main__': main()
