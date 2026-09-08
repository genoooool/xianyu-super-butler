#!/usr/bin/env python3
"""Offline composer interaction check against the built UI; blocks real network."""
import argparse
import io
import json
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import socket
import threading
import time

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from PIL import Image
from playwright.sync_api import expect, sync_playwright
import uvicorn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--static-dir', required=True, type=Path)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    app = FastAPI()
    app.mount('/static', StaticFiles(directory=args.static_dir))
    app.mount('/', StaticFiles(directory=args.static_dir, html=True))
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    picture = io.BytesIO(); Image.new('RGB', (200, 120), '#e9c64c').save(picture, format='PNG')
    sends = []; images = []; errors = []
    phrase = dict(id=1, category='默认', title='图片答复', content='这里是图片内容说明', image_ids=['fixture-image'])
    phrase.update(sort_order=0, enabled=True, use_count=0)
    stored = [phrase.copy()]
    mutations = []
    saved_groups = []
    fail_next = [False]
    payloads = {
        '/verify': dict(authenticated=True, user_id=1, is_admin=False),
        '/system-settings/public': dict(registration_enabled='false'),
        '/system-settings': {},
        '/desktop/notifications/status': dict(available=False, active=False),
        '/desktop/credentials': dict(available=False, saved=False),
        '/cookies/details': [dict(id='a', nickname='测试店铺', enabled=False)],
        '/items': dict(items=[]), '/message-filters': dict(data=[]),
        '/chat/handoffs': dict(entries=[]),
        '/chat/accounts': dict(data=[dict(accountId='a', displayName='测试店铺', connected=True)]),
        '/quick-phrases': dict(data=[phrase]),
        '/chat/conversations/a': dict(data=dict(conversations=[dict(cid=cid, otherUserId='buyer', otherUserName=name,
            lastMessageSummary='隔离预览', lastMessageTime=1, unreadCount=0) for cid, name in [('one', '测试买家一'), ('two', '测试买家二')]], hasMore=False)),
    }
    def route(request):
        url = request.request.url
        if not url.startswith(base + '/'):
            request.abort(); return
        path = url[len(base):].split('?')[0]
        if path == '/user-settings/quick_phrase_groups':
            if request.request.method == 'PUT':
                saved_groups[:] = json.loads(request.request.post_data_json['value'])
            request.fulfill(json=dict(value=json.dumps(saved_groups))); return
        if path.startswith('/quick-phrases'):
            method = request.request.method
            if method == 'GET':
                rows = sorted(stored, key=lambda row: (row['category'], row['sort_order'], row['id']))
                request.fulfill(json=dict(success=True, data=rows)); return
            if path.endswith('/use'):
                request.fulfill(json=dict(success=True)); return
            assert request.request.headers.get('authorization') == 'Bearer offline-test'
            if fail_next[0]:
                fail_next[0] = False
                request.fulfill(status=503, json=dict(detail='模拟保存失败')); return
            raw = b'Content-Type: ' + request.request.headers['content-type'].encode() + b'\r\n\r\n' + request.request.post_data_buffer
            form = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True).decode() for part in BytesParser(policy=default).parsebytes(raw).iter_parts()}
            if 'sort_order' in form: form['sort_order'] = int(form['sort_order'])
            if 'image_ids' in form: form['image_ids'] = json.loads(form['image_ids'])
            if 'enabled' in form: form['enabled'] = form['enabled'] == 'true'
            if method == 'PUT':
                row = next(row for row in stored if row['id'] == int(path.rsplit('/', 1)[1])); row.update(form)
            else:
                stored.append(dict(id=max(row['id'] for row in stored) + 1, enabled=True, use_count=0, **form))
            mutations.append(form)
            request.fulfill(json=dict(success=True)); return
        if path.startswith('/chat/send/'):
            sends.append(path); request.fulfill(status=500, json={}); return
        if 'fixture-image' in path:
            images.append(request.request.headers.get('authorization'))
            request.fulfill(body=picture.getvalue(), content_type='image/png'); return
        if path == '/' or path.startswith('/static/'):
            request.continue_(); return
        request.fulfill(json=payloads.get(path, dict(success=True, data=dict(messages=[], hasMore=False))))
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline: time.sleep(.05)
        assert server.started
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.browser, headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1000))
            page.route('**/*', route)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("localStorage.setItem('auth_token','offline-test'); localStorage.setItem('active_page','messages')")
            page.goto(base)
            page.get_by_text('测试买家一', exact=True).first.click()
            trigger = page.get_by_title('快捷短语', exact=True)
            popup = page.locator('#chat-quick-phrases')
            draft = page.get_by_placeholder('输入消息', exact=True)
            for theme in ('dark', 'light'):
                page.evaluate('(theme)=>{document.documentElement.dataset.theme=theme; document.documentElement.classList.toggle("dark",theme==="dark")}', theme)
                trigger.click()
                expect(popup.get_by_alt_text('回复图片')).to_be_visible()
                assert popup.get_by_alt_text('回复图片').evaluate('(img)=>img.complete && img.naturalWidth>0')
                expect(popup).to_contain_text(phrase['content'])
                page.locator('footer').screenshot(path=str(args.output_dir / f'composer-{theme}.png'))
                popup.screenshot(path=str(args.output_dir / f'thumbnails-{theme}.png'))
                draft.click(); expect(popup).to_have_count(0)
                expect(draft).to_have_css('outline-style', 'none')
                trigger.click(); page.keyboard.press('Escape'); expect(popup).to_have_count(0)
                expect(trigger).to_be_focused()
                trigger.click(); page.keyboard.press('Tab'); page.keyboard.press('Tab')
                expect(popup).to_have_count(0)
                trigger.click(); popup.get_by_role('button').click()
                expect(popup).to_have_count(0); expect(draft).to_be_focused()
                expect(draft).to_have_value(phrase['content'])
                expect(page.locator('footer').get_by_alt_text('回复图片')).to_be_visible()
                page.locator('footer').screenshot(path=str(args.output_dir / f'draft-{theme}.png'))
                draft.fill('')
            trigger.click(); page.get_by_text('测试买家二', exact=True).click()
            expect(popup).to_have_count(0); expect(draft).to_have_value('')
            page.set_viewport_size(dict(width=390, height=844))
            trigger.click(); expect(popup).to_be_visible()
            box = popup.bounding_box(); assert box['x'] >= 0 and box['x'] + box['width'] <= 390
            # Persisted grouped order, real HTML drag events, and bounded scrolling.
            page.keyboard.press('Escape'); page.set_viewport_size(dict(width=1440, height=1000))
            stored.extend(dict(id=i, category='售前' if i < 14 else '售后', title=f'短语 {i}', content='内容 ' * 60,
                               image_ids=[], sort_order=0, enabled=True, use_count=0) for i in range(2, 26))
            page.get_by_role('button', name='系统设置', exact=True).click()
            page.get_by_role('tab', name='快捷短语', exact=True).click()
            group = page.get_by_role('region', name='分组 售前', exact=True)
            expect(group.locator('[data-phrase-id]')).to_have_count(12)
            page.get_by_label('拖动排序 短语 4', exact=True).drag_to(page.locator('[data-phrase-id="2"]'))
            expect(group.locator('[data-phrase-id]').first).to_have_attribute('data-phrase-id', '4')
            page.reload()
            page.get_by_role('button', name='系统设置', exact=True).click()
            page.get_by_role('tab', name='快捷短语', exact=True).click()
            expect(group.locator('[data-phrase-id]').first).to_have_attribute('data-phrase-id', '4')
            page.get_by_label('移动分组 短语 4', exact=True).select_option('售后')
            expect(page.get_by_role('region', name='分组 售后', exact=True).locator('[data-phrase-id]').last).to_have_attribute('data-phrase-id', '4')
            page.get_by_role('button', name='＋ 新建分组', exact=True).click()
            page.get_by_label('分组名称', exact=True).fill('常用')
            page.get_by_role('button', name='创建分组', exact=True).click()
            expect(page.get_by_label('短语分组', exact=True)).to_have_value('常用')
            assert '常用' in saved_groups
            page.reload()
            page.get_by_role('button', name='系统设置', exact=True).click()
            page.get_by_role('tab', name='快捷短语', exact=True).click()
            page.get_by_label('短语分组', exact=True).select_option('常用')
            page.get_by_label('短语标题', exact=True).fill('新分组短语')
            page.get_by_label('话术内容', exact=True).fill('保存后聊天可用')
            page.get_by_role('button', name='添加', exact=True).click()
            expect(page.get_by_role('region', name='分组 常用', exact=True)).to_contain_text('新分组短语')
            page.get_by_label('筛选短语分组').get_by_role('button', name='全部').click()
            fail_next[0] = True
            page.get_by_label('下移 短语 2', exact=True).click()
            expect(page.get_by_text('保存未完成：', exact=False)).to_be_visible()
            expect(group.locator('[data-phrase-id]').first).to_have_attribute('data-phrase-id', '2')
            page.screenshot(path=str(args.output_dir / 'phrase-groups-settings.png'))
            page.get_by_role('button', name='消息中心', exact=True).click()
            trigger.click()
            expect(popup.locator('[data-quick-phrase-id]')).to_have_count(26)
            box = popup.bounding_box(); assert box['height'] <= 321
            scroller = popup.locator('.overflow-y-auto')
            assert scroller.evaluate('(el)=>el.scrollHeight > el.clientHeight')
            scroller.evaluate('(el)=>el.scrollTop=el.scrollHeight')
            assert scroller.evaluate('(el)=>el.scrollTop>0')
            popup.get_by_role('button', name='售后', exact=True).click()
            expect(popup.locator('[data-quick-phrase-id]').last).to_have_attribute('data-quick-phrase-id', '4')
            popup.screenshot(path=str(args.output_dir / 'phrase-groups-chat.png'))
            page.keyboard.press('Escape')
            page.set_viewport_size(dict(width=390, height=844))
            page.get_by_text('测试买家一', exact=True).first.click(); trigger.click()
            box = popup.bounding_box(); assert box['height'] <= 321 and box['y'] >= 0
            assert images and all(value == 'Bearer offline-test' for value in images)
            assert not sends and not errors, (sends, errors)
            browser.close()
        result = dict(status='passed', themes=['dark', 'light'], thumbnail_auth=True,
                      groups=True, drag_order_after_reload=True, cross_group_move=True, bounded_scroll=True,
                      dismissal=['outside', 'escape', 'tab', 'selection', 'conversation'], send_requests=len(sends), errors=errors)
        (args.output_dir / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
    finally:
        server.should_exit = True; thread.join(5)


if __name__ == '__main__': main()
