"""Recognize platform-generated seller cards, never ordinary seller text/images."""
import json


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def is_platform_seller_card(message):
    detail = _object(_object(_object(message).get('1')).get('10'))
    ext = _object(detail.get('extJson'))
    if str(ext.get('contentType')) != '26':
        return False
    tag = _object(detail.get('bizTag'))
    source = str(tag.get('sourceId', ''))
    task = tag.get('taskName')
    return ((source.startswith('RED_FLOWER:') and task == '求送小红花-卖家')
            or (source.startswith('C2C:') and task == '期待评价_卖家'))
