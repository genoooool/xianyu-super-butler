#!/usr/bin/env python3
"""Real built UI with offline release states; never contacts GitHub or buyers."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from playwright.sync_api import expect, sync_playwright


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self,*_): pass


def main():
    p=argparse.ArgumentParser(); p.add_argument('--static-dir',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True); args=p.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=False)
    state=dict(available=True,phase='idle',version='1.0.0',latest_version='',notes='',error='',progress=0)
    actions=[]; errors=[]; unexpected=[]
    fixtures={'/verify':dict(authenticated=True,is_admin=True,user_id=1), '/system-settings/public':{},
        '/desktop/notifications/status':dict(available=False), '/desktop/credentials':dict(available=False),
        '/quick-phrases':dict(data=[]), '/system-settings':dict(data={})}
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(args.static_dir)))
    threading.Thread(target=server.serve_forever,daemon=True).start(); base=f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless=True)
            page=browser.new_page(viewport=dict(width=1440,height=1000)); page.on('pageerror',lambda e:errors.append(str(e)))
            def route(route):
                req=route.request; path=urlsplit(req.url).path
                if not req.url.startswith(base+'/'): unexpected.append(req.url); route.abort(); return
                if path=='/desktop/updates/status': route.fulfill(json=state); return
                if path=='/desktop/updates/action' and req.method=='POST':
                    value=req.post_data_json; actions.append(value)
                    state.update(phase='checking' if value['action']=='check' else 'downloading',error='')
                    route.fulfill(json=state); return
                if req.method!='GET': unexpected.append((req.method,path)); route.abort(); return
                if path in fixtures: route.fulfill(json=fixtures[path]); return
                if path.startswith('/static/'): route.continue_(url=base+req.url[len(base+'/static'):]); return
                if path=='/' or path.startswith(('/assets/','/favicon')): route.continue_(); return
                unexpected.append(path); route.fulfill(status=404,json={})
            page.route('**/*',route)
            page.add_init_script("localStorage.setItem('auth_token','offline'); localStorage.setItem('active_page','about')")
            page.goto(base)
            check=page.get_by_role('button',name='检查更新',exact=True)
            expect(check).to_be_enabled(); check.click(); expect(check).to_be_disabled()
            state.update(phase='unpublished'); expect(page.get_by_text('尚未发布更新清单',exact=True)).to_be_visible()
            check.click(); state.update(phase='error',error='检查失败，请检查网络后重试')
            expect(page.get_by_role('alert')).to_have_text('检查失败，请检查网络后重试')
            state.update(phase='available',latest_version='1.0.1',notes='修复商品筛选与消息发送状态。\n保留原有数据。',error='')
            download=page.get_by_role('button',name='下载并升级',exact=True); expect(download).to_be_visible()
            download.click(); page.get_by_role('button',name='取消',exact=True).click()
            assert not any(x['action']=='install' for x in actions)
            page.screenshot(path=str(args.output_dir/'update-light.png'),animations='disabled')
            page.emulate_media(color_scheme='dark'); page.screenshot(path=str(args.output_dir/'update-dark.png'),animations='disabled')
            page.set_viewport_size(dict(width=390,height=844)); page.screenshot(path=str(args.output_dir/'update-mobile.png'),animations='disabled')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            download.click(); page.get_by_role('button',name='确认升级',exact=True).dblclick()
            expect(page.get_by_text('正在下载并校验安装包…',exact=True)).to_be_visible()
            assert [x for x in actions if x['action']=='install']==[dict(action='install',version='1.0.1')]
            state.update(progress=65); expect(page.get_by_text('65%',exact=True)).to_be_visible()
            state.update(phase='available',error='当前仍有操作进行中或登录已失效，请稍后再试')
            expect(page.get_by_role('alert')).to_contain_text('仍有操作')
            state.update(phase='incompatible',error='此版本需要调整数据格式，暂不支持应用内安装')
            expect(download).to_have_count(0)
            state.update(phase='current',error=''); expect(page.get_by_text('当前已是最新正式版本',exact=True)).to_be_visible()
            page.evaluate("localStorage.setItem('active_page','settings')")
            # Remove the init script override by navigating through the app's sidebar.
            page.set_viewport_size(dict(width=1440,height=1000))
            page.get_by_role('button',name='系统设置',exact=True).click()
            page.get_by_role('tab',name='软件更新',exact=True).click()
            expect(page.get_by_role('region',name='软件更新')).to_be_visible()
            expect(page.get_by_text('公告 JSON 地址',exact=True)).to_have_count(0)
            assert not errors and not unexpected,(errors,unexpected)
            (args.output_dir/'result.json').write_text(json.dumps(dict(status='passed',actions=actions,external_requests=unexpected),ensure_ascii=False,indent=2)+'\n')
            print('Update UI passed',flush=True); browser.close()
    finally: server.shutdown(); server.server_close()


if __name__=='__main__': main()
