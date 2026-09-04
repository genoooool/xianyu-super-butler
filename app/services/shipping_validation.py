"""Local seller checks and content-free classification of platform shipping replies."""
import re


def shipping_owner_error(db, order, cookie_id, cookies_str):
    """Workspace-user access alone does not establish the platform seller role."""
    if not order or str(order.get('cookie_id') or '') != str(cookie_id or ''):
        return '订单与当前店铺不一致，已停止发货，请核对订单归属'
    item_id = order.get('item_id')
    if not item_id:
        return '订单缺少商品ID，无法确认卖家归属'
    cookies = dict(part.strip().split('=', 1) for part in (cookies_str or '').split(';') if '=' in part)
    seller_id = cookies.get('unb', '')
    if not seller_id:
        return '无法确认闲鱼账号身份，请检查该店铺登录状态'
    if str(order.get('buyer_id') or '') == seller_id:
        return '这笔订单被归到了买家账号，不能替卖家发货；请先修正订单所属店铺'
    if not db.get_item_info(cookie_id, item_id):
        return '无法确认商品属于该店铺，已停止发货；请先同步商品并核对订单归属'
    return None


def shipping_response_result(response, http_status):
    """Never expose arbitrary platform text/tokens or treat HTTP 200 as success."""
    values = response.get('ret') if isinstance(response, dict) else None
    values = values if isinstance(values, list) else []
    codes = []
    for value in values[:8]:
        code = value.split('::', 1)[0] if isinstance(value, str) else ''
        codes.append(code if re.fullmatch(r'(?:SUCCESS|PERMISSION_ERROR|FAIL_[A-Z0-9_]{1,80})', code) else '<unrecognized>')
    success = (type(http_status) is int and 200 <= http_status < 300
               and values == ['SUCCESS::调用成功'] and response.get('success') is not False
               and not response.get('error'))
    data = response.get('data') if isinstance(response, dict) else None
    if isinstance(data, dict) and (data.get('success') is False or data.get('error')):
        success = False
    if success:
        return {'success': True, 'assessment': 'success_by_current_rule', 'ret_codes': codes}
    if 'PERMISSION_ERROR' in codes:
        error = '闲鱼拒绝操作：用户无权限操作，请核对这笔订单的卖家账号'
        code = 'PERMISSION_ERROR'
    elif any(code in {'FAIL_SYS_SESSION_EXPIRED', 'FAIL_SYS_USER_VALIDATE', 'FAIL_SYS_LOGIN_CANCEL'} for code in codes):
        error = '闲鱼登录校验未通过，请检查该店铺登录状态后再核对订单'
        code = next(code for code in codes if code.startswith('FAIL_'))
    elif any(code in {'FAIL_SYS_TOKEN_EXPIRED', 'FAIL_SYS_TOKEN_EXOIRED', 'FAIL_SYS_TOKEN_EMPTY'} for code in codes):
        error = '闲鱼账号令牌已失效，请刷新该店铺登录状态后再核对订单'
        code = next(code for code in codes if code.startswith('FAIL_'))
    elif any(code.startswith('FAIL_') for code in codes):
        code = next(code for code in codes if code.startswith('FAIL_'))
        error = f'闲鱼拒绝确认发货（{code}），请先核对平台订单'
    else:
        code = 'SHIPPING_UNCONFIRMED'
        error = '闲鱼发货结果未确认，请先检查闲鱼订单状态，勿重复完整发货'
    return {'success': False, 'error': error, 'code': code, 'ret_codes': codes,
            'assessment': 'unknown' if code == 'SHIPPING_UNCONFIRMED' else 'rejected'}
