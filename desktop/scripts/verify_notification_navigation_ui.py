"""UI fixtures only: no seller credentials, platform requests or buyer sends."""
from urllib.parse import urlparse

from playwright.sync_api import expect


def verify_navigation_ui(page, context, base, native, output_dir):
    accounts = [dict(accountId='shop-a', displayName='验收店铺甲', connected=True),
                dict(accountId='shop-b', displayName='验收店铺乙', connected=True)]
    page.route('**/chat/accounts', lambda r: r.fulfill(json={'success': True, 'data': accounts}))
    page.route('**/cookies/details', lambda r: r.fulfill(json=[{'id': a['accountId'], 'remark': a['displayName'], 'enabled': True} for a in accounts]))

    def conversations(route):
        account = urlparse(route.request.url).path.split('/')[-1]
        route.fulfill(json={'success': True, 'data': {'hasMore': False, 'conversations': [dict(
            cid='first', rawCid='first', otherUserId='ordinary-' + account, otherUserName='普通会话-' + account,
            lastMessageSummary='列表第一页', lastMessageTime=1, unreadCount=0)]}})
    page.route('**/chat/conversations/*?*', conversations)
    history = []

    def messages(route):
        account, cid = urlparse(route.request.url).path.split('/')[-2:]
        history.append((account, cid))
        sender_id = 'buyer-a' if account == 'shop-a' and cid == 'outside-page' else 'fixture'
        sender_name = '历史恢复买家甲' if sender_id == 'buyer-a' else '验收买家'
        route.fulfill(json={'success': True, 'data': {'hasMore': False, 'messages': [dict(
            messageId=account + cid, senderId=sender_id, senderName=sender_name, isSelf=False,
            type='text', text='历史验证-' + account + '-' + cid, images=[], time=1000)]}})
    page.route('**/chat/messages/*/*?*', messages)
    # The real native-only endpoint queues a generic click for the logged-in UI.
    assert context.request.post(base + '/desktop/notifications/activate', headers=native, data={'target': ''}).json()['queued']
    expect(page.get_by_role('combobox', name='消息账号')).to_have_value('__all__', timeout=20000)
    expect(page.get_by_text('历史验证-shop-a-first', exact=True)).to_be_visible()

    # Specific IDs below are fixtures; account ownership is tested in Python.
    pending = []
    def activation(route):
        route.fulfill(json={'navigation': pending.pop(0) if pending else None})
    page.route('**/desktop/notifications/activation', activation)
    pending.append(dict(id='click-b', account_id='shop-b', chat_id='outside-page', buyer_id='buyer-b', buyer_name='通知买家乙'))
    expect(page.get_by_role('combobox', name='消息账号')).to_have_value('__all__')
    expect(page.get_by_text('历史验证-shop-b-outside-page', exact=True)).to_be_visible()
    expect(page.get_by_role('heading', name='通知买家乙', exact=True)).to_be_visible()
    page.get_by_title('刷新账号和会话', exact=True).click()
    expect(page.get_by_text('历史验证-shop-b-outside-page', exact=True)).to_be_visible()
    page.screenshot(path=str(output_dir / 'notification-chat.png'), animations='disabled')
    # Identical cid in another shop must fetch that shop, never reuse old history.
    pending.append(dict(id='click-a', account_id='shop-a', chat_id='outside-page', buyer_id='buyer-a'))
    expect(page.get_by_role('combobox', name='消息账号')).to_have_value('__all__')
    expect(page.get_by_text('历史验证-shop-a-outside-page', exact=True)).to_be_visible()
    expect(page.get_by_role('heading', name='历史恢复买家甲', exact=True)).to_be_visible()
    expect(page.get_by_role('heading', name='通知买家乙', exact=True)).not_to_be_visible()
    expect(page.get_by_text('历史验证-shop-b-outside-page', exact=True)).not_to_be_visible()
    assert ('shop-b', 'outside-page') in history and ('shop-a', 'outside-page') in history
    page.get_by_text('普通会话-shop-a', exact=True).click()
    expect(page.get_by_text('历史验证-shop-a-first', exact=True)).to_be_visible()
    page.get_by_title('刷新账号和会话', exact=True).click()
    expect(page.get_by_text('历史验证-shop-a-first', exact=True)).to_be_visible()

    # Order nickname primary + stable numeric ID secondary, both searchable.
    orders = [dict(order_id='fixture-order', cookie_id='shop-a', item_id='fixture-item', buyer_id='123456789',
                   buyer_name='测试买家昵称', quantity=1, amount='0.10', order_status='shipped', item_title='验收商品')]
    page.route('**/api/orders?*', lambda r: r.fulfill(json={'success': True, 'data': orders, 'total': 1, 'page': 1, 'total_pages': 1, 'status_counts': {'all': 1, 'shipped': 1}}))
    page.get_by_role('button', name='订单管理', exact=True).click()
    expect(page.get_by_text('测试买家昵称', exact=True)).to_be_visible()
    expect(page.get_by_text('ID：123456789', exact=True)).to_be_visible()
    page.locator('input[placeholder="搜索订单号/商品/买家..."]:visible').fill('测试买家昵称')
    expect(page.get_by_text('测试买家昵称', exact=True)).to_be_visible()
    page.screenshot(path=str(output_dir / 'order-nickname.png'), animations='disabled')
    print('Passed: native generic activation; notification cached nickname/history nickname/cross-shop/exact-cid/outside-first-page/refresh/manual-selection; order nickname + ID + search (offline UI fixtures)', flush=True)
