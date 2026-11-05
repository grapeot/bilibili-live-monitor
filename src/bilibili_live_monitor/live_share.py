"""单场只读静态快照：仅公开聚合指标和标注，不导出原始消息。"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SERIES = ('entries', 'online', 'fans', 'gifts')


def epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('时间必须带时区')
    return parsed.timestamp()


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def fingerprint(segment: dict) -> str:
    return hashlib.sha256(json.dumps({k: segment.get(k) for k in ('seq', 'start_utc', 'end_utc', 'text')},
                                   sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def scope_source(source: dict, start: str, end: float) -> dict:
    lower = epoch(start)
    return {
        'raw': [row for row in source['raw'] if lower <= epoch(row[0]) <= end],
        'items': [row for row in source['items'] if lower <= epoch(row[1]) <= end],
        'segments': [s for s in source['segments']
                     if lower <= epoch(s['start_utc']) <= epoch(s['end_utc']) <= end],
    }


def read_source(database: Path, room_id: str, start: str) -> dict:
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        raw = [(at, cmd, json.loads(data)) for at, cmd, data in db.execute(
            'SELECT received_utc,command,data FROM raw_events WHERE room_id=? AND received_utc>=? ORDER BY id',
            (room_id, start))]
        items = [(kind, at, {**json.loads(data), '_record_id': ident}) for ident, kind, at, data in db.execute(
            'SELECT id,kind,at_utc,data FROM timeline WHERE room_id=? AND at_utc>=? ORDER BY id', (room_id, start))]
        segments = [json.loads(data) for data, in db.execute(
            "SELECT data FROM segments WHERE room_id=? AND json_extract(data,'$.start_utc')>=? ORDER BY seq",
            (room_id, start))]
    return {'raw': raw, 'items': items, 'segments': segments}


def followup_candidates(source: dict, start: str, rules: dict | None = None) -> list[dict]:
    """实际灯牌和盲盒购买建待办；同单多抽中结果只触发一次盲盒跟进。"""
    rules = rules or {}
    lamp_ids = set(rules.get('lamp_ids', ['31164', '34001']))
    blind_names = set(rules.get('blind_gift_names', []))
    people, seen = {}, set()
    guards = {}
    for _, at, data in sorted(source['items'], key=lambda row: epoch(row[1])):
        uid, level = data.get('sender_uid'), data.get('guard_level')
        if isinstance(uid, str) and type(level) is int and level in (0, 1, 2, 3):
            guards[uid] = level
    for kind, at, data in source['items']:
        gift = data.get('gift')
        if kind != 'gift' or not isinstance(gift, dict):
            continue
        blind = gift.get('blind_gift')
        blind_result = isinstance(blind, dict) and bool(blind)
        original_name = blind.get('original_gift_name') if blind_result else gift.get('gift_name')
        has_blind = blind_result or str(gift.get('gift_name', '')).endswith('盲盒')
        is_blind = (rules.get('include_blind', True) and has_blind
                    and (not blind_names or original_name in blind_names))
        is_lamp = (str(gift.get('gift_id')) in lamp_ids
                   and gift.get('gift_name') == rules.get('lamp_name', '粉丝团灯牌') and not has_blind)
        if (gift.get('event_type') not in {'SEND_GIFT', 'SEND_GIFT_V2'} or not (is_lamp or is_blind)
                or type(gift.get('num')) is not int or gift['num'] < 1
                or data.get('identity_status') == 'unresolved'):
            continue
        uid = data.get('sender_uid') or gift.get('sender_uid')
        if not isinstance(uid, str) or not uid.isdigit() or int(uid) <= 0:
            continue
        if gift.get('sender_uid') and gift['sender_uid'] != uid:
            continue
        identity = gift.get('event_id') or data.get('source_key') or data.get('_record_id')
        key = (uid, identity, None if is_blind else gift.get('batch_index'))
        if identity is None or key in seen:
            continue
        seen.add(key)
        name = data.get('author')
        name = ''.join(c for c in name if c.isprintable())[:80] if isinstance(name, str) else ''
        item = people.setdefault(uid, {'id': hashlib.sha256(f'{start}:{uid}'.encode()).hexdigest()[:24],
                                       'uid': uid, 'name': name or '未提供昵称的送礼观众',
                                       'first_gift_at': at, 'last_gift_at': at,
                                       'gift_count': 0, 'lamp_count': 0, 'blind_orders': 0})
        item['gift_count'] += 1 if is_blind else gift['num']
        item['blind_orders'] += int(is_blind)
        item['lamp_count'] += gift['num'] if is_lamp else 0
        if epoch(at) < epoch(item['first_gift_at']):
            item['first_gift_at'] = at
        if epoch(at) > epoch(item['last_gift_at']):
            item['last_gift_at'] = at
    return [item for uid, item in people.items() if item['blind_orders']
            or not rules.get('exclude_guard_lamps', True) or guards.get(uid) not in (1, 2, 3)]


def followup_evidence(source: dict) -> list[dict]:
    evidence = []
    for segment in source['segments']:
        if segment.get('is_final') and segment.get('text'):
            evidence.append({'id': 't:' + str(segment['seq']) + ':' + fingerprint(segment)[:16],
                             'at': segment['end_utc'], 'start': segment['start_utc'],
                             'seq': segment['seq'], 'kind': 'transcript', 'text': segment['text']})
    for kind, at, data in source['items']:
        if kind == 'danmaku' and data.get('_record_id') is not None:
            evidence.append({'id': 'm:' + str(data['_record_id']), 'at': at, 'kind': 'danmaku',
                             'uid': data.get('sender_uid'), 'name': data.get('author'),
                             'text': data.get('text', '')})
    return evidence


def public_followups(source: dict, start: str, annotations: dict, overrides: dict | None = None,
                     approved_songs: tuple[str, ...] = (), rules: dict | None = None) -> list[dict]:
    candidates = followup_candidates(source, start, rules)
    evidence = {e['id']: e for e in followup_evidence(source)}
    completions = annotations.get('completed_followups', [])
    done = {}
    if isinstance(completions, list):
        for decision in completions:
            if isinstance(decision, dict) and isinstance(decision.get('id'), str):
                done[decision['id']] = decision.get('evidence', [])
    result = []
    for item in candidates:
        override = (overrides or {}).get(item['uid'], {})
        if override.get('status') == 'excluded':
            continue
        refs = done.get(item['id'], [])
        valid = (isinstance(refs, list) and 0 < len(refs) <= 6
                  and all(isinstance(ref, str) and ref in evidence
                          and epoch(evidence[ref]['at']) >= epoch(item['first_gift_at']) for ref in refs)
                  and any(epoch(evidence[ref]['at']) >= epoch(item['last_gift_at']) for ref in refs))
        # A completion must cite the relevant viewer's chat or a transcript. A different viewer's
        # chat alone cannot satisfy this person's follow-up. Semantics remain the AI's judgment.
        linked = valid and any(evidence[ref]['kind'] == 'transcript'
                               or evidence[ref].get('uid') == item['uid'] for ref in refs)
        status = 'done' if linked else 'pending'
        if override.get('status') == 'done':
            status = 'done'
        elif override.get('status') == 'awaiting_response':
            # A fresh acknowledgement may resolve this, but an older model answer cannot.
            status = 'done' if linked and all(epoch(evidence[ref]['at']) > epoch(override['at'])
                                              for ref in refs) else 'awaiting_response'
        request = {}
        requests = annotations.get('song_requests', [])
        for candidate in requests if isinstance(requests, list) else []:
            if not isinstance(candidate, dict) or candidate.get('id') != item['id']:
                continue
            cited = candidate.get('evidence')
            if (candidate.get('song') in approved_songs and candidate.get('stage') in {'accepted', 'performed'}
                    and isinstance(cited, list) and 0 < len(cited) <= 6
                    and all(isinstance(ref, str) and ref in evidence
                            and epoch(evidence[ref]['at']) >= epoch(item['first_gift_at']) - 300 for ref in cited)
                    and any(epoch(evidence[ref]['at']) >= epoch(item['last_gift_at']) for ref in cited)
                     and any(evidence[ref]['kind'] == 'transcript' or evidence[ref].get('uid') == item['uid'] for ref in cited)):
                request = {'request_song': candidate['song'], 'request_stage': candidate['stage']}
        if request.get('request_stage') == 'performed':
            status = 'done'
        result.append({'name': item['name'], 'first_gift_at': item['first_gift_at'],
                       'gift_count': item['gift_count'], 'lamp_count': item['lamp_count'],
                       'blind_orders': item['blind_orders'], 'status': status, **request})
    return result


def make_snapshot(source: dict, *, start: str, now: str, label: str, phase: str,
                  health: dict, annotations: dict | None = None,
                  approved_songs: tuple[str, ...] = (), followup_overrides: dict | None = None,
                  followup_rules: dict | None = None) -> dict:
    lower, upper = epoch(start), epoch(now)
    source = scope_source(source, start, upper)
    buckets = {name: {} for name in SERIES}
    observed_minutes = set()
    latest = {}
    for at, command, payload in source['raw']:
        second = epoch(at)
        if not lower <= second <= upper:
            continue
        observed_minutes.add(int(second // 60) * 60)
        data = payload.get('data') or {}
        values = {}
        if command == 'ONLINE_RANK_COUNT':
            values = {'online': data.get('online_count')}
        elif command == 'ROOM_REAL_TIME_MESSAGE_UPDATE':
            values = {'fans': data.get('fans')}
        for name, value in values.items():
            if number(value) is not None:
                buckets[name][int(second // 60) * 60] = [second, value]
                latest[name] = {'value': value, 'at': iso(second)}
    counts = {'entries': 0, 'gifts': 0, 'danmaku': 0}
    for kind, at, _ in source['items']:
        second = epoch(at)
        if not lower <= second <= upper:
            continue
        name = {'entry': 'entries', 'gift': 'gifts', 'danmaku': 'danmaku'}.get(kind)
        if name:
            counts[name] += 1
            if name in buckets:
                bucket = int(second // 60) * 60
                buckets[name][bucket] = [bucket, buckets[name].get(bucket, [bucket, 0])[1] + 1]
    for name in ('entries', 'gifts'):
        for minute in observed_minutes:
            buckets[name].setdefault(minute, [minute, 0])
    segments = {s['seq']: s for s in source['segments']
                if lower <= epoch(s['start_utc']) <= epoch(s['end_utc']) <= upper}
    activity = []
    annotations = annotations or {}
    reviews = annotations.get('reviews', [])
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, dict) or not isinstance(review.get('seq'), int):
            continue
        segment = segments.get(review.get('seq'))
        if not segment or review.get('fingerprint') != fingerprint(segment):
            continue
        label_value = review.get('label')
        if label_value not in {'singing', 'speaking', 'mixed', 'unknown'}:
            continue
        song = review.get('song')
        if not isinstance(song, str) or song not in approved_songs:
            song = None
        # Only explicit, bounded fields leave the local review file.
        activity.append({'start': segment['start_utc'], 'end': segment['end_utc'],
                          'label': label_value, 'song': song,
                          'music_source': review.get('music_source') if review.get('music_source') in
                          {'probable_live', 'recording', 'uncertain', 'none'} else 'uncertain',
                         'confidence': review.get('confidence') if review.get('confidence') in {'high', 'medium', 'low'} else 'low'})
    safe_health = {}
    for key in ('recording', 'asr_enabled', 'asr_ready', 'has_error', 'message_connected'):
        safe_health[key] = health.get(key) if isinstance(health.get(key), bool) else None
    for key in ('queue_depth', 'recording_growth_seconds', 'recording_growth_bytes'):
        safe_health[key] = number(health.get(key))
    def safe_time(value):
        try:
            second = epoch(value)
            return iso(second) if lower <= second <= upper else None
        except (TypeError, ValueError, AttributeError, OverflowError):
            return None
    for key in ('latest_transcript_at', 'latest_message_at'):
        safe_health[key] = safe_time(health.get(key))
    from .activity_summary import summarize_activity, summarize_gifters, summarize_session
    return {'schema_version': 1, 'label': label, 'session_start': start, 'updated_at': now,
            'phase': phase, 'series': {k: sorted(v.values()) for k, v in buckets.items()},
            'latest': latest, 'counts': counts, 'health': safe_health,
            'activity': activity, 'ai_updated_at': safe_time(annotations.get('updated_at')),
            'coverage': [[s['start_utc'], s['end_utc']] for s in segments.values()],
            'followups': public_followups(source, start, annotations, followup_overrides, approved_songs, followup_rules),
            'activity_summary': summarize_activity(source, activity, upper, completed=phase == 'completed'),
            'session_summary': summarize_session(source),
            'gift_summary': summarize_gifters(source),
            'notes': ['在线人数为平台上报口径；入场、礼物为本系统收到的事件数。',
                       '歌唱与歌名是AI候选判断，不代表已完成听审；不公开原始转写或UID，仅显示送礼汇总与点歌跟进所需公开昵称。']}


def render_snapshot(snapshot: dict, *, navigation=None) -> str:
    """实时页复用离线趋势页的 SVG、版式和悬浮交互。"""
    import html
    from urllib.parse import urlsplit
    from zoneinfo import ZoneInfo

    from .trends_render import render

    def esc(value):
        return html.escape(str(value), quote=True)

    def clock(value):
        return datetime.fromtimestamp(value, ZoneInfo('Asia/Shanghai')).strftime('%H:%M:%S')
    lo, hi = epoch(snapshot['session_start']), epoch(snapshot['updated_at'])
    hi = max(lo + 1, hi)
    bands, songs = [], []
    cursor = lo
    for start, end in sorted((epoch(a), epoch(b)) for a, b in snapshot['coverage']):
        if start > cursor:
            bands.append({'start': cursor, 'end': start, 'label': 'unknown', 'reason': '无ASR覆盖'})
        cursor = max(cursor, end)
    if cursor < hi:
        bands.append({'start': cursor, 'end': hi, 'label': 'unknown', 'reason': '无ASR覆盖'})
    for item in sorted(snapshot['activity'], key=lambda item: epoch(item['start'])):
        if item['label'] != 'singing' or item.get('music_source') != 'probable_live':
            continue
        start, end = epoch(item['start']), epoch(item['end'])
        bands.append({'start': start, 'end': end, 'label': 'singing'})
    from .activity_summary import content_blocks
    for block in content_blocks(snapshot['activity'], include_segments=True):
        if block['kind'] != 'song' or not block['title']:
            continue
        spans = []
        for part in block['segments']:
            if spans and part['start'] <= spans[-1]['end'] + 2:
                spans[-1]['end'] = max(spans[-1]['end'], part['end'])
            else:
                spans.append(dict(part))
        songs.append({'id': f'song-{len(songs)}', 'title': block['title'], 'segments': spans})
    charts = []
    for field, name, color, mode in [('entries', '每分钟入场事件', '#2563eb', 'line'),
                                     ('online', '在线人数', '#0891b2', 'stock'),
                                     ('fans', '关注人数', '#8b5cf6', 'stock'),
                                     ('gifts', '每分钟礼物事件', '#e79024', 'bar')]:
        points = []
        for at, value in snapshot['series'][field]:
            start, end = max(lo, at), min(hi, math.floor(at / 60) * 60 + 60)
            if end < start:
                continue
            points.append({'t': start if mode == 'bar' else at if mode == 'stock' else (start + end) / 2,
                           'end': end, 'v': value, 'partial': mode != 'stock' and end - start < 60,
                           'label': f'{clock(at)}：{value}'})
        latest = snapshot['latest'].get(field)
        note = f'最新 {latest["value"]} · {clock(epoch(latest["at"]))}' if latest else '每分钟收到的事件数'
        charts.append({'field': field, 'name': name, 'color': color, 'mode': mode,
                       'points': points, 'note': note})
    followups = ''.join('<li>' + ('<s>' if item['status'] == 'done' else '') + esc(item['name']) +
                        ('</s>' if item['status'] == 'done' else '') +
                        f' · {clock(epoch(item["first_gift_at"]))} · ' +
                        '、'.join(([f'灯牌×{item["lamp_count"]}'] if item.get('lamp_count') else []) +
                                 ([f'盲盒{item["blind_orders"]}次'] if item.get('blind_orders') else [])) + ' · ' +
                         {'done': '已完成', 'awaiting_response': '已cue，待回应', 'pending': '待确认'}[item['status']] +
                         (f' · {esc(item["request_song"])}（' + {'accepted': '已接单', 'performed': '已唱'}[item['request_stage']] + '）'
                          if item.get('request_song') else '') + '</li>'
                        for item in snapshot['followups'])
    status = {'completed': '本场已结束', 'finalizing': '本场已结束，正在整理最终版', 'live': '直播中'}[snapshot['phase']]
    if snapshot['phase'] == 'live':
        status = '<span class="live-pill">'+status+'</span>'
    header = (f'<p>{clock(lo)}—{clock(hi)}（北京时间） · {status}<br>'
               f'更新于 {clock(hi)}</p>')
    destinations = {}
    if navigation and Path(navigation).is_file():
        destinations = json.loads(Path(navigation).read_text())
        links = []
        for key, label in [('home_url', '← 返回主页'), ('songs_url', '歌曲搜索')]:
            target = destinations.get(key, '')
            if key == 'songs_url' and not target:
                continue
            parsed = urlsplit(target)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('场次导航必须为有效HTTPS地址')
            links.append(f'<a href="{esc(target)}">{label}</a>')
        header = '<nav class="archive-nav">' + ' · '.join(links) + '</nav>' + header
    footer = ('绿色：歌曲相关片段；其他内容不着色；灰色：暂无内容记录。'
              '<section id="followups"><h2>灯牌与盲盒点歌跟进</h2><ul>' +
              (followups or '<li>暂无灯牌或盲盒点歌记录</li>') + '</ul></section>')
    rows = []
    for item in snapshot.get('activity_summary', []):
        title = (item['title'] or '歌名待确认') if item['kind'] == 'song' else '杂谈'
        song_attr = f' data-song-catalog-title="{esc(title)}"' if item['kind'] == 'song' else ''
        change = item['followers_change']
        fans = '—' if change is None else f'{change:+d}' if change else '0'
        count = str(item['gift_count']) + ('+' if item['gift_count_unknown'] else '')
        amount = f'{item["gift_amount_yuan"]:.2f}' + ('+' if item['gift_amount_unknown'] else '')
        state = {'complete': '', 'collecting': '统计中', 'partial': '尾窗未满'}[item['status']]
        rows.append(f'<tr><td>{clock(epoch(item["start"]))}–{clock(epoch(item["end"]))}</td>'
                    f'<td{song_attr}>{esc(title)}</td><td>{fans}</td><td>{count}</td><td>{amount}</td><td>{state}</td></tr>')
    footer += ('<section id="activity-summary"><h2>歌曲与杂谈</h2>'
               '<p>统计至每段结束后30秒；相邻段可能重叠。涨粉为净变化，金额为平台报告金额，非主播实收。'
               '“+”表示另有金额或数量未知的礼物。</p><div class="activity-table"><table>'
               '<thead><tr><th>时间段</th><th>内容</th><th>涨粉</th><th>礼物数</th><th>金额（元）</th><th>状态</th></tr></thead>'
               '<tbody>' + (''.join(rows) or '<tr><td colspan="6">暂无已识别的内容片段</td></tr>') +
               '</tbody></table></div></section>')
    if snapshot.get('content_mode') == 'talk':
        footer = ''
    totals = snapshot.get('session_summary', {})
    from .activity_summary import render_gifters
    footer += render_gifters(snapshot.get('gift_summary', []))
    change = totals.get('followers_change')
    fans = '—' if change is None else f'{change:+d}' if change else '0'
    count = str(totals.get('gift_count', 0)) + ('+' if totals.get('gift_count_unknown') else '')
    amount = f'{totals.get("gift_amount_yuan", 0):.2f}' + ('+' if totals.get('gift_amount_unknown') else '')
    watched = str(totals['watched']) if totals.get('watched') is not None else '—'
    footer += ('<section id="session-summary"><h2>本场汇总</h2><div class="activity-table"><table>'
               '<thead><tr><th>累计涨粉</th><th>礼物总数</th><th>礼物总金额（元）</th><th>看过人数</th></tr></thead>'
               f'<tbody><tr><td>{fans}</td><td>{count}</td><td>{amount}</td><td>{watched}</td></tr></tbody>'
               '</table></div><p>按本场已收到的数据汇总；看过人数采用平台累计值。</p></section>')
    data = {'title': snapshot['label'], 'timezone': 'Asia/Shanghai', 'since': iso(lo), 'until': iso(hi),
            'summary': {'gaps': [], 'unique_entry_uids': 0, 'gift_events': snapshot['counts']['gifts']}}
    revision = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    from .song_navigation import with_song_catalog_links, with_song_navigation
    page = with_song_navigation(render(data, bands, songs, charts=charts, header=header, footer=footer,
                  show_song_list=False, refresh_revision=revision if snapshot['phase'] != 'completed' else None))
    page = with_song_catalog_links(page, destinations.get('songs_url'))
    if destinations.get('audience_url'):
        from .audience_navigation import with_audience_links
        page = with_audience_links(page, destinations['audience_url'])
    return page


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
    temporary.replace(path)
