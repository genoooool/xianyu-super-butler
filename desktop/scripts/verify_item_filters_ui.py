#!/usr/bin/env python3
"""Product filtering on the built UI, with local fixtures and zero write requests."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--static-dir', required=True, type=Path)
    parser.add_argument('--browser', required=True)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--expect-old-bug', action='store_true')
    args = parser.parse_args(); args.output_dir.mkdir(parents=True, exist_ok=False)
    items = [dict(id=f'{account}-{i}', cookie_id=account,
        item_id='777777777' if i == 3 else f'{prefix}{i:03}',
        item_title='星星 GitHub 套餐' if i == 3 else f'{account.upper()}店商品 {i:02}',
        item_price='10', item_detail='不在商品标题搜索范围的内部资料',
        listing_status='off_shelf' if i == count else 'on_sale')
        for account, prefix, count in [('a', '10000', 10), ('b', '20000', 8)] for i in range(1, count+1)]
    configs = [dict(cookie_id=account, item_id=item_id, enabled=True, is_multi_spec=False,
                    variant_count=1, configured_count=1, complete=True, delivery_times=0)
               for account, item_id in [('a', '777777777'), ('b', '20000004'), ('b', 'missing-product')]]
    fixtures = {
        '/verify': dict(authenticated=True, user_id=1, is_admin=False),
        '/system-settings/public': dict(registration_enabled='false'),
        '/desktop/notifications/status': dict(available=False, active=False),
        '/desktop/credentials': dict(available=False, saved=False),
        '/cookies/details': [dict(id=account, nickname=f'店铺 {account.upper()}', enabled=False) for account in ('a', 'b', 'c')],
        '/items': dict(items=items), '/cards': [], '/item-delivery-configs': dict(configs=configs),
        '/delivery-rules': [dict(id=1, cookie_id='a', item_id='10000001', keyword='商品一', card_id=1, enabled=True)],
        '/blacklist': dict(success=True, entries=[]),
    }
    reads, writes, unexpected, errors = [], [], [], []
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(args.static_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.browser, headless=True)
            page = browser.new_page(viewport=dict(width=1440, height=1000))
            page.on('pageerror', lambda error: errors.append(str(error)))
            def route_request(route):
                request = route.request; path = urlsplit(request.url).path
                if not request.url.startswith(base+'/'): unexpected.append(request.url); route.abort(); return
                if request.method != 'GET': writes.append((request.method, path)); route.fulfill(status=405, json={}); return
                if path in fixtures: reads.append(path); route.fulfill(json=fixtures[path]); return
                if path.startswith('/delivery-block-rules/'): reads.append(path); route.fulfill(json=dict(success=True, rules=[])); return
                if path.startswith('/static/'):
                    route.continue_(url=base+request.url[len(base+'/static'):]); return
                if path == '/' or path.startswith(('/assets/', '/favicon')): route.continue_(); return
                unexpected.append(path); route.fulfill(status=404, json={})
            page.route('**/*', route_request)
            page.add_init_script("localStorage.setItem('auth_token','offline-test'); localStorage.setItem('active_page','items')")
            page.goto(base)
            account = page.get_by_label('选择账号', exact=True)
            rows = page.locator('article:visible')
            expect(account).to_have_value('a')
            if args.expect_old_bug:
                expect(rows).to_have_count(16)
                expect(rows.filter(has_text='店铺 B')).to_have_count(7)
                page.screenshot(path=str(args.output_dir/'old-account-mismatch.png'), animations='disabled')
                report = dict(status='old_bug_reproduced', selected_account='a', visible_other_store_rows=7)
            else:
                search = page.get_by_role('searchbox', name='搜索商品名称或商品 ID')
                expect(rows).to_have_count(9); expect(rows.filter(has_text='店铺 B')).to_have_count(0)
                expect(page.get_by_text('已配置专属发货 2 件', exact=True)).to_be_visible()
                account.select_option('b'); expect(rows).to_have_count(7)
                expect(rows.filter(has_text='店铺 A')).to_have_count(0)
                expect(page.get_by_text('已配置专属发货 1 件', exact=True)).to_be_visible()
                account.select_option(''); expect(rows).to_have_count(16)
                expect(page.get_by_role('button', name='同步商品', exact=True)).to_be_disabled()

                # Filter the entire selected dataset before pagination, resetting to page one.
                page.get_by_label('每页显示数量').select_option('10')
                page.get_by_role('button', name='下一页', exact=True).click(); expect(rows).to_have_count(6)
                search.fill('10000001'); expect(rows).to_have_count(1)
                expect(rows.get_by_role('heading', name='A店商品 01', exact=True)).to_be_visible()
                expect(page.get_by_text('第 1 页 / 共 1 页（1 件）', exact=True)).to_be_visible()
                page.get_by_label('清空商品搜索', exact=True).click(); expect(rows).to_have_count(10)

                # Same title/ID in two accounts stays scoped; case and surrounding spaces are ignored.
                account.select_option('a'); search.fill('  github  '); expect(rows).to_have_count(1)
                expect(rows.filter(has_text='店铺 A')).to_have_count(1)
                page.screenshot(path=str(args.output_dir/'search-light.png'), animations='disabled')
                account.select_option('b'); expect(rows).to_have_count(1)
                expect(rows.filter(has_text='店铺 B')).to_have_count(1)
                search.fill('777777777'); expect(rows).to_have_count(1)
                account.select_option(''); expect(rows).to_have_count(2)
                search.fill('不存在的商品'); expect(rows).to_have_count(0)
                expect(page.get_by_text('未找到匹配商品', exact=True)).to_be_visible()
                page.get_by_role('button', name='清空搜索', exact=True).click(); expect(search).to_have_value('')
                search.fill('内部资料'); expect(rows).to_have_count(0)

                account.select_option('a'); search.fill('10000010'); expect(rows).to_have_count(0)
                page.get_by_role('checkbox', name='显示已下架（1）').check(); expect(rows).to_have_count(1)
                expect(rows.get_by_text('已下架', exact=True)).to_be_visible()
                page.get_by_label('清空商品搜索', exact=True).click(); expect(rows).to_have_count(10)
                page.get_by_role('checkbox', name='显示已下架（1）').uncheck(); expect(rows).to_have_count(9)

                # Protection already shares this selection. Returning must not show the old store.
                page.get_by_role('tab', name='发货保护与黑名单', exact=True).click()
                page.locator('#protection-account').select_option('b')
                page.get_by_role('tab', name=re.compile('商品与专属发货')).click()
                expect(account).to_have_value('b'); expect(rows).to_have_count(7)
                expect(rows.filter(has_text='店铺 A')).to_have_count(0)
                account.select_option('c'); expect(rows).to_have_count(0)
                expect(page.get_by_text('当前范围暂无商品', exact=True)).to_be_visible()
                account.select_option('a'); search.fill('星星')
                page.emulate_media(color_scheme='dark')
                page.screenshot(path=str(args.output_dir/'search-dark.png'), animations='disabled')
                page.set_viewport_size(dict(width=390, height=844)); search.click()
                page.screenshot(path=str(args.output_dir/'search-mobile.png'), animations='disabled')
                box = search.bounding_box(); assert box['x'] >= 0 and box['x'] + box['width'] <= 390
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert reads.count('/items') == 1, 'Search must not re-fetch platform products'
                report = dict(status='passed', initial_scope=True, cross_account=True, title_id_search=True,
                              pagination=True, off_shelf=True, protection_tab_selection=True, write_requests=writes)
            assert not writes and not unexpected and not errors, (writes, unexpected, errors)
            (args.output_dir/'result.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
            print(json.dumps(report, ensure_ascii=False)); browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == '__main__': main()
