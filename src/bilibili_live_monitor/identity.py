"""显式启用的昵称分析身份；不修改平台 UID，也不与历史 UID 身份合并。"""
import json


def nickname_key(room_id, name):
    if not isinstance(name, str) or not name.strip() or name in ('神秘人', '未命名用户'):
        return None
    return 'nickname:' + json.dumps([str(room_id), name], ensure_ascii=False, separators=(',', ':'))


def analysis_key(data):
    if data.get('sender_hidden'):
        return None
    if data.get('analysis_identity_basis') == 'nickname':
        room_id = data.get('room_id')
        return nickname_key(room_id, data.get('author')) if room_id else None
    uid = data.get('sender_uid') or (data.get('gift') or {}).get('sender_uid')
    if data.get('identity_status') == 'unresolved':
        return None
    if not isinstance(uid, bool) and str(uid or '').isdigit() and int(uid) > 0:
        return str(uid)
    return None
