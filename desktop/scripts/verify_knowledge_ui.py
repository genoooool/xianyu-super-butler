#!/usr/bin/env python3
"""Built UI + real knowledge router/SQLite; all seller/model endpoints are fixtures."""

import argparse
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.routers.ai_knowledge import create_ai_knowledge_router
from app.services.ai_knowledge import initialize_schema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--static-dir', required=True, type=Path)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    database = args.output_dir / 'knowledge-ui.sqlite3'
    if database.exists():
        raise RuntimeError('Use a fresh output directory; existing evidence is preserved')
    db = SimpleNamespace(conn=sqlite3.connect(database, check_same_thread=False), lock=threading.RLock())
    db.conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE cookies (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL);
        CREATE TABLE item_info (cookie_id TEXT, item_id TEXT);
        INSERT INTO users VALUES (1);
        INSERT INTO cookies VALUES ('store-a',1), ('store-b',1);
        INSERT INTO item_info VALUES ('store-a','product-1'),('store-b','product-1');
    """)
    initialize_schema(db.conn.cursor())
    db.conn.commit()

    def user(authorization: str = Header(default='')):
        if authorization != 'Bearer offline-test':
            raise HTTPException(401)
        return {'user_id': 1}

    app = FastAPI()
    app.include_router(create_ai_knowledge_router(user, db))

    @app.get('/verify')
    def verify():
        return {'authenticated': True, 'user_id': 1, 'is_admin': False}

    @app.get('/system-settings/public')
    def public():
        return {'registration_enabled': 'false'}

    @app.get('/desktop/notifications/status')
    def notifications():
        return {'available': False, 'active': False}

    @app.get('/desktop/credentials')
    def credentials():
        return {'available': False, 'saved': False}

    @app.get('/cookies/details')
    def accounts():
        return [{'id': f'store-{key}', 'nickname': f'测试店铺 {key.upper()}', 'enabled': False}
                for key in ('a', 'b')]

    @app.get('/items')
    def items():
        return {'items': [{'cookie_id': f'store-{key}', 'item_id': 'product-1',
                           'item_title': f'测试商品 {key.upper()}'} for key in ('a', 'b')]}

    @app.get('/ai-reply-settings/{cookie_id}')
    def settings(cookie_id: str):
        return {'ai_enabled': False}

    app.mount('/static', StaticFiles(directory=args.static_dir))
    app.mount('/', StaticFiles(directory=args.static_dir, html=True))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    base = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=args.browser, headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1100})
            page = context.new_page()
            errors, failed = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('response', lambda response: failed.append(response.url) if response.status >= 400 else None)
            page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
            page.add_init_script("localStorage.setItem('auth_token','offline-test'); localStorage.setItem('active_page','ai-reply')")
            page.goto(base)
            page.get_by_role('button', name='打开商品与店铺知识库', exact=True).click()
            scope = page.get_by_label('资料范围', exact=True)
            expect(scope).to_have_value('account')

            def save(scope_value, content):
                scope.select_option(scope_value)
                if scope_value == 'item':
                    page.get_by_label('知识所属商品', exact=True).select_option('product-1')
                page.get_by_label('知识主题', exact=True).fill('售后期限')
                page.get_by_label('知识触发词', exact=True).fill('售后, 能退吗')
                page.get_by_label('知识内容', exact=True).fill(content)
                page.get_by_role('button', name='保存知识资料', exact=True).click()
                expect(page.get_by_label('知识条目', exact=True)).to_contain_text(content)

            def preview(content, source):
                page.get_by_role('button', name='检索预览', exact=True).click()
                result = page.get_by_label('检索结果', exact=True)
                expect(result).to_contain_text(content)
                expect(result).to_contain_text('来源：' + source)
                assert result.locator('> div').count() == 1

            save('shared', '共用规则：七天内可联系售后。')
            save('account', 'A 店规则：三天内可联系售后。')
            save('item', 'A 商品规则：一天内可联系售后。')
            page.get_by_label('预览商品', exact=True).select_option('product-1')
            preview('A 商品规则', '商品专属')
            page.get_by_role('button', name='编辑 售后期限', exact=True).click()
            expect(page.get_by_label('知识主题', exact=True)).to_be_disabled()
            page.get_by_label('知识内容', exact=True).fill('A 商品规则：两天内可联系售后。')
            page.get_by_role('button', name='保存知识资料', exact=True).click()
            expect(page.get_by_label('知识条目', exact=True)).to_contain_text('两天内')
            preview('两天内', '商品专属')
            page.get_by_role('button', name='停用 售后期限', exact=True).click()
            expect(page.get_by_role('button', name='启用 售后期限', exact=True)).to_be_visible()
            preview('A 店规则', '店铺资料')
            page.get_by_role('button', name='启用 售后期限', exact=True).click()
            expect(page.get_by_role('button', name='停用 售后期限', exact=True)).to_be_visible()
            preview('两天内', '商品专属')
            while page.get_by_role('button', name='关闭提示', exact=True).count():
                page.get_by_role('button', name='关闭提示', exact=True).first.click()
            page.get_by_label('AI 知识库', exact=True).screenshot(path=str(args.output_dir / 'knowledge-product.png'), animations='disabled')

            page.get_by_label('当前账号').select_option('store-b')
            expect(scope).to_have_value('account')
            expect(page.get_by_label('知识条目', exact=True)).to_contain_text('暂无资料')
            preview('共用规则', '共用资料')
            save('account', 'B 店规则：五天内可联系售后。')
            preview('B 店规则', '店铺资料')
            page.reload()
            page.get_by_role('button', name='打开商品与店铺知识库', exact=True).click()
            preview('A 店规则', '店铺资料')
            page.get_by_label('预览商品', exact=True).select_option('product-1')
            preview('两天内', '商品专属')
            page.get_by_label('知识测试问题', exact=True).fill('天气怎么样')
            page.get_by_role('button', name='检索预览', exact=True).click()
            expect(page.get_by_role('status').filter(has_text='未匹配到资料')).to_be_visible()
            # Preview is inert, cancel is inert, confirm persists original text.
            scope.select_option('account')
            document = '# 激活说明\n' + '背景介绍。\n' * 500 + '\n# 故障处理\n激活失败时请提供错误截图。\n<script>window.importExecuted=true</script>'
            upload = page.get_by_label('选择知识文档', exact=True)
            upload.set_input_files({'name': '说明书.md', 'mimeType': 'text/markdown', 'buffer': document.encode()})
            expect(page.get_by_label('文档导入预览', exact=True)).to_be_visible()
            expect(page.get_by_label('知识条目', exact=True)).not_to_contain_text('说明书')
            assert page.evaluate('window.importExecuted') is None
            page.get_by_role('button', name='取消导入', exact=True).click()
            expect(page.get_by_label('文档导入预览', exact=True)).to_have_count(0)
            upload.set_input_files({'name': '说明书.md', 'mimeType': 'text/markdown', 'buffer': document.encode()})
            page.get_by_role('button', name='确认导入并启用', exact=True).click()
            expect(page.get_by_label('知识条目', exact=True)).to_contain_text('说明书')
            page.get_by_label('知识测试问题', exact=True).fill('激活失败错误截图')
            page.get_by_role('button', name='检索预览', exact=True).click()
            expect(page.get_by_label('检索结果', exact=True)).to_contain_text('激活失败时请提供错误截图')
            page.get_by_role('button', name='编辑 说明书', exact=True).click()
            expect(page.get_by_label('知识内容', exact=True)).to_have_value(document)
            page.get_by_label('AI 知识库', exact=True).screenshot(path=str(args.output_dir / 'document-import.png'), animations='disabled')
            page.set_viewport_size({'width': 900, 'height': 1000})
            page.wait_for_function("document.querySelector('aside').getBoundingClientRect().right <= 0")
            page.get_by_label('AI 知识库', exact=True).screenshot(path=str(args.output_dir / 'knowledge-narrow.png'), animations='disabled')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
            assert not errors, errors
            assert not failed, failed
            browser.close()
            print('Passed: default store scope, save/edit/persistence, product > store > shared, disable fallback, store isolation, no match, no overflow/JS/API errors; no model or buyer calls')
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        db.conn.close()
        assert not thread.is_alive(), 'test server did not stop'


if __name__ == '__main__':
    main()
