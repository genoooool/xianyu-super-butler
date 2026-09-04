#!/usr/bin/env python3
"""Isolated built UI verification: SQLite image/QA/phrase persistence, no model/buyer traffic."""
import argparse
import io
import json
import re
from pathlib import Path
import socket
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from PIL import Image
from playwright.sync_api import expect, sync_playwright
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.routers.ai_knowledge import create_ai_knowledge_router
from app.services.ai_knowledge import KnowledgeService, initialize_schema
from app.services.quick_phrases import QuickPhrases, initialize_schema as phrases_schema
from app.services.human_handoff import HumanHandoffs, initialize_schema as handoff_schema
from app.routers.human_handoff import create_human_handoff_router
from app.services.manual_reply import send_manual_reply


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
        CREATE TABLE item_info(cookie_id TEXT,item_id TEXT); INSERT INTO item_info VALUES('a','one'),('a','two'),('a','three'),('b','b-only');
        CREATE TABLE chat_quick_phrases(id INTEGER PRIMARY KEY,category TEXT DEFAULT '默认',title TEXT,content TEXT,
        sort_order INTEGER DEFAULT 0,enabled INTEGER DEFAULT 1,use_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);''')
    initialize_schema(db.conn.cursor()); phrases_schema(db.conn.cursor()); db.conn.commit()
    handoff_schema(db.conn.cursor()); db.conn.commit()
    handoffs=HumanHandoffs(db)
    phrases=QuickPhrases(db)
    def user(authorization: str=Header(default='')):
        if authorization!='Bearer offline-test': raise HTTPException(401)
        return {'user_id':1}
    app=FastAPI(); app.include_router(create_ai_knowledge_router(user,db))
    app.include_router(create_human_handoff_router(user,db,lambda *_: None))
    def add_get(path,value): app.get(path)(lambda: value)
    add_get('/verify',dict(authenticated=True,user_id=1,is_admin=False))
    add_get('/system-settings/public',dict(registration_enabled='false'))
    add_get('/desktop/notifications/status',dict(available=False,active=False))
    add_get('/desktop/credentials',dict(available=False,saved=False))
    add_get('/cookies/details',[dict(id='a',nickname='测试店铺 A',enabled=False,auto_confirm=True),dict(id='b',nickname='测试店铺 B',enabled=False)])
    add_get('/ai-reply-settings',{})
    add_get('/api/risk-control/status',dict(success=True,accounts=[]))
    add_get('/items',dict(items=[dict(cookie_id='a',item_id='one',item_title='测试商品：星星套餐',item_image='/static/test-product.png'),
        dict(cookie_id='a',item_id='two',item_title='测试商品：月亮套餐'),
        dict(cookie_id='a',item_id='three',item_title='测试商品：太阳套餐'),
        dict(cookie_id='b',item_id='b-only',item_title='其他店铺的商品')]))
    add_get('/default-replies/{cookie_id}',dict(enabled=False))
    add_get('/keywords/{cookie_id}',[])
    add_get('/reply-rules/{cookie_id}',[])
    add_get('/keywords-with-item-id/{cookie_id}',[])
    add_get('/system-settings',{})
    add_get('/message-filters',dict(data=[]))
    add_get('/chat/accounts',dict(data=[dict(accountId='a',displayName='测试店铺 A',connected=True),dict(accountId='b',displayName='测试店铺 B',connected=False)]))
    @app.get('/chat/conversations/{cookie_id}')
    def conversations(cookie_id: str):
        rows=[dict(cid='chat',otherUserId='buyer',otherUserName='离线测试买家',lastMessageSummary='想看报价图',lastMessageTime=1,unreadCount=1,itemId='one',itemImage='/static/broken-product.png')] if cookie_id=='a' else []
        return dict(data=dict(conversations=rows,hasMore=False))
    add_get('/chat/messages/{cookie_id}/{cid}',dict(data=dict(messages=[],hasMore=False)))
    @app.get('/quick-phrases')
    def list_phrases(include_disabled: bool=False): return dict(success=True,data=phrases.list(1,include_disabled))
    @app.post('/quick-phrases')
    async def create_phrase(request: Request):
        form=dict(await request.form()); return dict(success=True,id=phrases.save(1,form))
    @app.post('/quick-phrases/{phrase_id}/use')
    def use_phrase(phrase_id: int): return dict(success=phrases.use(1,phrase_id))
    sends=[]; send_success=True
    @app.post('/chat/send/{cookie_id}')
    async def record_send(cookie_id: str, request: Request):
        data=await request.json(); sends.append(data)
        if not send_success: return dict(success=False,message='离线模拟未确认')
        receipt={'headers':{'code':200},'body':{'messageId':f'offline-{len(sends)}'}}
        instance=SimpleNamespace(cookie_id=cookie_id,myid='seller',cookies_str='offline',
            send_im_text=AsyncMock(return_value=receipt),_send_im_request=AsyncMock(return_value=receipt))
        manager=AsyncMock()
        manager.__aenter__.return_value=SimpleNamespace(upload_reply_bytes=AsyncMock(return_value='https://img.alicdn.com/offline.png'))
        with patch.dict(sys.modules,{'utils.image_uploader':SimpleNamespace(ImageUploader=Mock(return_value=manager))}):
            result=await send_manual_reply(instance,db,1,data['cid'],data['to_user_id'],data['text'],data.get('image_ids',[]),lambda *_: None)
        return dict(success=True,message='离线模拟确认',data=result)
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
            page=context.new_page(); errors=[]; failed=[]; expected_failures=set()
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.on('response',lambda response: failed.append(response.url) if response.status>=400 and (response.url,response.status) not in expected_failures else None)
            page.route('**/*',lambda route: route.continue_() if route.request.url.startswith(base+'/') else route.abort())
            page.route('**/static/broken-product.png',lambda route: route.abort())
            page.route('**/static/test-product.png',lambda route: route.fulfill(body=output.getvalue(),content_type='image/png'))
            page.add_init_script("localStorage.setItem('auth_token','offline-test'); localStorage.setItem('active_page','auto-reply')")
            page.goto(base)
            page.get_by_role('button',name='添加意图回复',exact=True).click()
            modal=page.get_by_role('dialog',name='添加意图回复')
            expect(modal).to_be_visible()
            modal.get_by_label('回复范围',exact=True).select_option('item')
            modal.get_by_label('选择商品 测试商品：星星套餐',exact=True).check()
            modal.get_by_label('选择商品 测试商品：月亮套餐',exact=True).check()
            expect(modal.get_by_label('选择商品 其他店铺的商品',exact=True)).to_have_count(0)
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
            expect(row).to_contain_text('2 个商品')
            stored=KnowledgeService(db).list_entries(1)
            assert len(stored)==1 and stored[0]['item_ids']==['one','two']
            original_id=stored[0]['id']
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
            expect(modal.get_by_label('回复范围',exact=True)).to_be_enabled()
            expect(modal.get_by_label('QA问题意图')).to_be_enabled()
            expect(modal.get_by_label('选择商品 测试商品：星星套餐',exact=True)).to_be_checked()
            expect(modal.get_by_label('选择商品 测试商品：月亮套餐',exact=True)).to_be_checked()
            modal.get_by_label('QA问题意图').fill('套餐询价')
            modal.get_by_label('选择商品 测试商品：星星套餐',exact=True).uncheck()
            modal.get_by_label('搜索回复商品',exact=True).fill('太阳')
            modal.get_by_label('选择商品 测试商品：太阳套餐',exact=True).check()
            modal.get_by_label('搜索回复商品',exact=True).fill('')
            modal.get_by_label('QA固定答案').fill('点开商品购买页面查看对应价格哦亲。')
            page.set_viewport_size(dict(width=390,height=844))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(args.output_dir/'multiselect-edit-mobile.png'))
            page.set_viewport_size(dict(width=1440,height=1000))
            modal.screenshot(path=str(args.output_dir/'multiselect-edit-dark.png'))
            modal.get_by_role('button',name='保存固定回复').click()
            row=page.locator('article').filter(has_text='套餐询价')
            expect(row).to_contain_text('点开商品购买页面')
            assert KnowledgeService(db).list_entries(1)[0]['id']==original_id
            assert KnowledgeService(db).list_entries(1)[0]['item_ids']==['three','two']
            page.reload(); expect(row).to_contain_text('2 个商品')
            page.get_by_label('回复店铺',exact=True).select_option('b')
            expect(page.locator('article').filter(has_text='套餐询价')).to_have_count(0)
            page.get_by_label('回复店铺',exact=True).select_option('a')
            page.get_by_role('button',name='编辑 套餐询价',exact=True).click()
            modal=page.get_by_role('dialog',name='编辑固定回复')
            modal.get_by_label('回复范围',exact=True).select_option('account')
            expect(modal.get_by_label('回复所属商品',exact=True)).to_have_count(0)
            modal.get_by_role('button',name='保存固定回复').click()
            expect(row).to_contain_text('店铺')
            assert KnowledgeService(db).list_entries(1)[0]['id']==original_id
            assert KnowledgeService(db).list_entries(1)[0]['scope']=='account'
            page.reload(); expect(row).to_contain_text('店铺')
            page.get_by_role('tab',name='关键词回复').click()
            page.get_by_role('button',name='添加关键词回复',exact=True).click()
            modal=page.get_by_role('dialog',name='添加关键词回复')
            modal.get_by_label('QA问题意图').fill('你好')
            modal.get_by_label('添加回复图片').set_input_files(upload)
            expect(modal.get_by_alt_text('回复图片')).to_be_visible()
            modal.get_by_role('button',name='保存固定回复').click()
            expect(page.locator('article').filter(has_text='你好')).to_be_visible()
            page.get_by_role('button',name='删除 你好',exact=True).click()
            expect(page.get_by_role('alertdialog',name='删除固定回复')).to_be_visible()
            page.get_by_role('button',name='取消',exact=True).click()
            expect(page.locator('article').filter(has_text='你好')).to_be_visible()
            page.get_by_role('button',name='删除 你好',exact=True).click()
            page.get_by_role('button',name='确认删除',exact=True).click()
            expect(page.locator('article').filter(has_text='你好')).to_have_count(0)
            page.reload()
            page.get_by_role('tab',name='关键词回复').click()
            expect(page.locator('article').filter(has_text='你好')).to_have_count(0)
            assert len(KnowledgeService(db).list_entries(1))==1
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
            ai_switch=page.get_by_role('switch',name='当前会话 AI 自动回复')
            expect(ai_switch).to_have_attribute('aria-checked','true')
            expect(page.get_by_alt_text('商品图片')).to_have_attribute('src','/static/test-product.png')
            assert page.get_by_alt_text('商品图片').evaluate('(img)=>img.complete && img.naturalWidth>0')
            expect(ai_switch.locator('span').nth(1)).to_have_css('background-color','rgb(67, 136, 100)')
            page.screenshot(path=str(args.output_dir/'chat-product-and-ai-on.png'),animations='disabled')
            page.get_by_title('快捷短语',exact=True).click()
            page.get_by_role('button',name='[默认] 价格图片').click()
            expect(page.locator('img[alt="回复图片"]:visible')).to_be_visible()
            page.screenshot(path=str(args.output_dir/'message-image-draft.png'))
            page.get_by_role('button',name='发送',exact=True).click()
            expect(page.locator('[data-send-status="sent"] img[alt="回复图片"]:visible')).to_have_count(1)
            composer=page.locator('footer:visible')
            expect(composer.get_by_label('添加回复图片')).to_have_count(0)
            assert len(sends)==1 and sends[0]['text']=='' and len(sends[0]['image_ids'])==1
            expect(ai_switch).to_have_attribute('aria-checked','false')
            expect(ai_switch.locator('span').nth(1)).to_have_css('background-color','rgb(164, 91, 91)')
            page.screenshot(path=str(args.output_dir/'message-after-send.png'))

            # Failed payloads now remain in bubbles, not in an easily repeated draft.
            composer.get_by_title('添加图片',exact=True).click()
            expect(composer.get_by_label('添加回复图片')).to_have_count(1)
            composer.get_by_label('添加回复图片').set_input_files(upload)
            expect(composer.get_by_alt_text('回复图片')).to_be_visible()
            composer.get_by_placeholder('输入消息',exact=True).fill('保留这份图片草稿')
            send_success=False
            composer.get_by_role('button',name='发送',exact=True).click()
            expect(page.locator('[data-send-status="unconfirmed"]')).to_have_count(1)
            expect(page.locator('[data-send-status="unconfirmed"]').get_by_text('保留这份图片草稿',exact=True)).to_be_visible()
            expect(composer.get_by_placeholder('输入消息',exact=True)).to_have_value('')
            expect(composer.get_by_label('添加回复图片')).to_have_count(0)
            assert len(sends)==2 and len(sends[1]['image_ids'])==1
            page.screenshot(path=str(args.output_dir/'message-unconfirmed-draft.png'))

            # Local takeover state must remain visible independently of unread counts/platform paging.
            ai_switch.click()
            expect(ai_switch).to_have_attribute('aria-checked','true')
            expect(page.get_by_role('alertdialog')).to_have_count(0)
            state=handoffs.state(1,'a','chat')
            first=handoffs.begin(1,'a','chat',state['revision'],state['resumed_ms']+1,'unclear','buyer','离线测试买家','one')
            handoffs.finish_send(first,'confirmed')
            handoffs.begin(1,'a','not-in-platform-page',0,1,'unknown','buyer2','列表外买家','one')
            handoffs.begin(1,'b','chat',0,1,'unknown','buyer','另一店买家','')
            expect(ai_switch).to_have_attribute('aria-checked','false')
            expect(page.get_by_text('转人工话术已收到平台发送回执。',exact=True)).to_be_visible()
            while page.get_by_label('关闭提示',exact=True).count(): page.get_by_label('关闭提示',exact=True).first.click()
            ai_switch.click(trial=True)
            page.screenshot(path=str(args.output_dir/'handoff-dark.png'),animations='disabled')
            page.reload()
            page.get_by_role('button',name='消息中心',exact=True).click()
            page.get_by_text('离线测试买家',exact=True).first.click()
            expect(ai_switch).to_have_attribute('aria-checked','false')
            page.emulate_media(color_scheme='light')
            page.screenshot(path=str(args.output_dir/'handoff-light.png'))

            # Failed and confirmed replies both keep automatic replies off.
            composer.get_by_placeholder('输入消息',exact=True).fill('人工回复未确认')
            composer.get_by_role('button',name='发送',exact=True).click()
            expect(page.locator('[data-send-status="unconfirmed"]')).to_have_count(1)  # A page reload clears local outbox only.
            assert len(sends)==3 and len(handoffs.pending(1))==3
            expect(ai_switch).to_have_attribute('aria-checked','false')
            composer.get_by_placeholder('输入消息',exact=True).fill('')
            page.get_by_title('快捷短语',exact=True).click()
            page.get_by_role('button',name='[默认] 价格图片').click()
            send_success=True
            composer.get_by_role('button',name='发送',exact=True).click()
            expect(ai_switch).to_have_attribute('aria-checked','false')
            expect(composer.get_by_label('添加回复图片')).to_have_count(0)
            assert len(sends)==4 and len(handoffs.pending(1))==2
            assert handoffs.state(1,'a','chat')['reason']=='manual_reply'
            page.screenshot(path=str(args.output_dir/'handoff-manual-stays-off.png'))

            # Only the explicit switch reopens; no confirm modal and no old-message replay.
            expect(ai_switch).to_be_enabled()
            ai_switch.click()
            expect(ai_switch).to_have_attribute('aria-checked','true')
            expect(page.get_by_role('alertdialog')).to_have_count(0)
            state=handoffs.state(1,'a','chat')
            first=handoffs.begin(1,'a','chat',state['revision'],state['resumed_ms']+1,'unclear','buyer','离线测试买家','one')
            handoffs.finish_send(first,'confirmed')
            expect(ai_switch).to_have_attribute('aria-checked','false')
            while page.get_by_label('关闭提示',exact=True).count(): page.get_by_label('关闭提示',exact=True).first.click()
            assert len(handoffs.pending(1))==3
            expect(ai_switch).to_be_enabled()
            ai_switch.click()
            expect(ai_switch).to_have_attribute('aria-checked','true')
            expect(page.get_by_role('alertdialog')).to_have_count(0)
            assert len(handoffs.pending(1))==2
            page.get_by_text('列表外买家',exact=True).first.click()
            expect(ai_switch).to_have_attribute('aria-checked','false')
            expect(page.get_by_text('转人工话术发送结果未确认，请先查看原会话，避免重复发送。',exact=True)).to_be_visible()
            page.set_viewport_size(dict(width=390,height=844))
            expect(ai_switch).to_be_visible()
            while page.get_by_label('关闭提示',exact=True).count(): page.get_by_label('关闭提示',exact=True).first.click()
            ai_switch.click(trial=True)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(args.output_dir/'handoff-mobile.png'),animations='disabled')
            page.set_viewport_size(dict(width=1440,height=1000))
            page.get_by_label('消息账号').select_option('b')
            expect(page.get_by_text('另一店买家',exact=True).first).to_be_visible()
            expect(page.get_by_text('列表外买家',exact=True)).to_have_count(0)
            page.get_by_text('另一店买家',exact=True).first.click()
            expect(ai_switch).to_have_attribute('aria-checked','false')
            # The same cid in A is on; B remains off even after a page reload.
            assert handoffs.control(1,'a','chat')['enabled'] is True
            assert handoffs.control(1,'b','chat')['enabled'] is False
            page.get_by_label('消息账号').select_option('a')
            page.get_by_text('离线测试买家',exact=True).first.click()
            expect(ai_switch).to_have_attribute('aria-checked','true')
            # Both sources unavailable: clean Package icon, no browser broken image.
            page.route('**/static/test-product.png',lambda route: route.abort())
            page.reload()
            page.get_by_role('button',name='消息中心',exact=True).click()
            page.get_by_text('离线测试买家',exact=True).first.click()
            expect(page.get_by_role('img',name='暂无商品图片')).to_be_visible()
            expect(page.get_by_alt_text('商品图片')).to_have_count(0)
            expect(ai_switch).to_have_attribute('aria-checked','true')
            page.screenshot(path=str(args.output_dir/'chat-product-fallback.png'),animations='disabled')
            # Slow polling must not overwrite a newer click, even after its PUT has completed.
            control_url=base+'/chat/handoffs/a/chat'
            held=[]
            def hold_read(route):
                if route.request.method=='GET': held.append(route)
                else: route.continue_()
            old_state=handoffs.control(1,'a','chat')
            page.route(control_url,hold_read)
            page.wait_for_timeout(2200)
            assert held
            ai_switch.click()
            expect(ai_switch).to_have_attribute('aria-checked','false')
            held.pop(0).fulfill(json=old_state)
            page.wait_for_timeout(100)
            expect(ai_switch).to_have_attribute('aria-checked','false')
            page.unroute(control_url,hold_read)
            for route in held: route.fulfill(json=old_state)
            # A conflicting update is reconciled, never blindly retried or confirmed by a modal.
            saved=handoffs.control(1,'a','chat')
            handoffs.set_enabled(1,'a','chat',False,saved['revision'])
            expected_failures.add((control_url,409))
            ai_switch.click()
            expect(page.get_by_role('alert').filter(has_text='开关更新未确认')).to_be_visible()
            expect(ai_switch).to_have_attribute('aria-checked','false')
            expect(page.get_by_role('alertdialog')).to_have_count(0)
            # A failed state read displays unknown/disabled, not a deceptively green switch.
            expected_failures.add((control_url,503))
            page.route(control_url,lambda route: route.fulfill(status=503,json={'detail':'offline test'}))
            page.reload()
            page.get_by_role('button',name='消息中心',exact=True).click()
            page.get_by_text('离线测试买家',exact=True).first.click()
            expect(ai_switch).to_be_disabled()
            expect(ai_switch).to_have_attribute('title', re.compile('状态暂不可用'))
            page.unroute(control_url)
            expect(ai_switch).to_be_enabled()
            expect(ai_switch).to_have_attribute('aria-checked','false')
            assert len(sends)==4, 'Resume and unconfirmed sends must never automatically replay a buyer message'
            page.get_by_role('button',name='账号管理',exact=True).click()
            account=page.locator('article').filter(has_text='测试店铺 A')
            expect(account.get_by_text('自动确认发货',exact=True)).to_be_visible()
            account.get_by_title('编辑账号',exact=True).click()
            expect(page.get_by_text('自动发货流程发送全部卡券后，在闲鱼确认发货；不替买家确认收货',exact=True)).to_be_visible()
            expect(page.get_by_text('自动确认收货',exact=True)).to_have_count(0)
            page.screenshot(path=str(args.output_dir/'account-auto-confirm-label.png'))
            assert not errors, errors
            assert not failed, failed
            print(json.dumps(dict(status='passed',qa_entries=len(KnowledgeService(db).list_entries(1)),phrases=len(phrases.list(1)),offline_send_requests=len(sends),console_errors=errors,http_errors=failed),ensure_ascii=False))
            browser.close()
    finally:
        server.should_exit=True; thread.join(timeout=10); db.conn.close()


if __name__=='__main__': main()
