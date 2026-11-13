"""将单场GUARD_BUY候选绑定到唯一原始续舰消息，不依赖昵称猜身份或硬编码素材号。"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .gift_assets import canonical, positive
from .gift_media import absolute_time, session_config


def guard_event(session, event_id):
    root, config = session_config(session)
    manifest = root / 'offline/manifest.json'
    cutoff = (json.loads(manifest.read_text())['cutoff'] if manifest.exists()
              else datetime.now(timezone.utc).timestamp())
    database = Path(config['database']).resolve(strict=True)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT at_utc,data FROM timeline WHERE id=? AND room_id=? AND kind=?',
                         (positive(event_id), str(config['room_id']), 'gift')).fetchone()
        if not row or not absolute_time(config['start_utc']) <= absolute_time(row[0]) < cutoff:
            raise ValueError('事件不属于所选房间或场次')
        gift = json.loads(row[1]).get('gift', {})
        if gift.get('event_type') != 'GUARD_BUY' or gift.get('time_source') != 'event':
            raise ValueError('事件绑定目前仅支持有平台时间的GUARD_BUY')
        stamp = absolute_time(row[0])
        lower, upper = [datetime.fromtimestamp(stamp+n, timezone.utc).isoformat() for n in (-2, 10)]
        rows = db.execute('SELECT id,data FROM raw_events WHERE room_id=? AND command=? '
                          'AND received_utc>=? AND received_utc<? ORDER BY id LIMIT 101',
                          (str(config['room_id']), 'USER_TOAST_MSG_V2', lower, upper)).fetchall()
    if len(rows) > 100:
        raise ValueError('续舰原始消息窗口过密，不能唯一绑定')
    matches = []
    expected = (str(gift.get('sender_uid')), int(stamp), positive(gift['gift_id']),
                positive(gift['num']), positive(gift['guard_level']))
    for ident, body in rows:
        raw = json.loads(body).get('data', {})
        try:
            sender, guard, payment = raw['sender_uinfo'], raw['guard_info'], raw['pay_info']
            actual = (str(sender['uid']), int(guard['start_time']), positive(raw['gift_info']['gift_id']),
                      positive(payment['num']), positive(guard['guard_level']))
            if actual == expected:
                matches.append((ident, raw))
        except (KeyError, TypeError, ValueError):
            continue
    if len(matches) != 1:
        raise ValueError('不能唯一绑定USER_TOAST_MSG_V2；不猜测昵称或动画')
    ident, raw = matches[0]
    effect = positive(raw.get('effect_info', {}).get('ship_effect_id'))
    name = raw['sender_uinfo'].get('base', {}).get('name')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('续舰消息缺少动态昵称')
    return {'room_id': positive(config['room_id']), 'event_id': positive(event_id), 'at': row[0],
            'gift_id': expected[2], 'count': expected[3], 'gift_name': gift['gift_name'],
            'effect_id': effect, 'username': name, 'raw_event_id': ident,
            'evidence_sha256': hashlib.sha256(canonical({'timeline': json.loads(row[1]), 'raw': raw})).hexdigest()}
