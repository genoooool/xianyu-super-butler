"""Exercise the official keep-login button targeting in an offline browser fixture."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from playwright.async_api import async_playwright
from utils.desktop_qr_login import confirm_keep_login_prompt


async def run(args):
    args.output_dir.mkdir(parents=True, exist_ok=False)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=args.browser, headless=True)
        try:
            page = await browser.new_page()
            html = '''<h1>保持登录</h1><p>保持登录，下次访问无需重新登录</p>
                <ACTION class="keep-login-btn" onclick="window.choice='keep';document.body.innerHTML='已保持登录'">保持</ACTION>
                <button onclick="window.choice='decline'">不保持（4秒后自动关闭）</button>'''
            markup = html.replace('ACTION','button')
            async def route(request):
                await request.fulfill(content_type='text/html; charset=utf-8',body=markup)
            await page.route('**/*',route)
            for tag in ('button','div'):
                markup = html.replace('ACTION',tag)
                await page.goto('https://passport.goofish.com/offline-test')
                self_test = await confirm_keep_login_prompt(page)
                assert self_test and await page.evaluate('window.choice') == 'keep'
                assert not await confirm_keep_login_prompt(page)
            markup = html.replace('ACTION','button')
            await page.goto('https://unrelated.invalid/offline-test')
            assert not await confirm_keep_login_prompt(page)
            assert await page.evaluate('window.choice') is None
            markup = '<p>保存登录信息</p><button>保持登录</button>'
            await page.goto('https://www.goofish.com/offline-test')
            assert not await confirm_keep_login_prompt(page)
            result = {'status':'passed','positive_button_only':True,'div_button':True,
                      'no_double_click':True,'untrusted_frame_ignored':True,'normal_page_ignored':True}
            (args.output_dir/'result.json').write_text(json.dumps(result,indent=2))
            print(json.dumps(result))
        finally:
            await browser.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser',required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    asyncio.run(run(parser.parse_args()))
