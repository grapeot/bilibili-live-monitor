"""礼物批次：消息身份去重与录像区间取并集，事件仍在共同时间轴上分别播放。"""
import hashlib
import json
import math
import sqlite3
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

from . import gift_render
from .archive_media import archived_input, file_stat
from .gift_assets import (
    cache_default,
    cached_asset,
    canonical,
    normalize_catalog,
    select_effect,
    sha_file,
    write_json,
)
from .gift_event import guard_event
from .gift_media import absolute_time, dimensions, number, select_rows, session_config, source_clips
from .trailer import probe, run

EFFECT_KEYS = ('asset_path', 'asset_sha256', 'asset_version', 'gift_id', 'count',
               'overlay', 'dynamic', 'event_binding')


def merge_windows(entries, gap=0):
    """对相交或相接区间取并集；相同礼物/相邻时刻不构成消息重复。"""
    number(gap, maximum=60)
    groups = []
    for item in sorted(entries, key=lambda e: (e['start'], e['event_id'])):
        if not groups or item['start'] > groups[-1]['end'] + gap + 1e-7:
            groups.append({'start': item['start'], 'end': item['end'], 'events': [item]})
        else:
            groups[-1]['end'] = max(groups[-1]['end'], item['end'])
            groups[-1]['events'].append(item)
    return groups


def overrides(values):
    result = {}
    for value in values or []:
        ident, data = value.split('=', 1)
        ident = int(ident)
        if ident <= 0 or ident in result:
            raise ValueError('事件覆盖参数ID须为不重复的正整数')
        result[ident] = data
    return result


def make_batch(args):
    session, config = session_config(args.session)
    include_tail = getattr(args, 'include_recorded_tail', False)
    scope = gift_render.events(session, include_recorded_tail=include_tail)
    enumerated = scope['events']
    regions, aspects = overrides(args.event_region), overrides(args.event_aspect)
    if (regions.keys() | aspects.keys()) - {e['event_id'] for e in enumerated}:
        raise ValueError('覆盖参数引用了不属于所选场次的事件')
    canvas = dimensions(args.canvas)
    before, after = number(args.before), number(args.after)
    origin = absolute_time(config['start_utc'])
    catalog = normalize_catalog(json.loads(args.catalog.read_text())) if args.catalog else None
    entries, windows, seen, blind_members = [], [], {}, {}
    database = Path(config['database']).resolve(strict=True)
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        for event in enumerated:
            row = db.execute('SELECT source_key,data FROM timeline WHERE id=? AND room_id=?',
                             (event['event_id'], str(config['room_id']))).fetchone()
            gift = json.loads(row[1])['gift']
            item = {**event, 'status': 'candidate'}
            entries.append(item)
            # 缺少平台身份时只以本条数据库ID识别，不按昵称、数量或时间近似吞事件。
            key = row[0] or f'timeline:{event["event_id"]}'
            identity = hashlib.sha256(canonical([config['room_id'], key])).hexdigest()
            fingerprint = hashlib.sha256(canonical({
                'at': absolute_time(event['at']), 'gift_id': str(event['gift_id']), 'count': event['count'],
                'sender_uid': str(gift.get('sender_uid')), 'guard_level': gift.get('guard_level'),
                'blind_box': (gift.get('blind_gift') or {}).get('original_gift_id')})).hexdigest()
            item['identity_sha256'] = identity
            if identity in seen:
                previous_id, previous_fingerprint = seen[identity]
                if fingerprint != previous_fingerprint:
                    raise ValueError('相同事件身份的内容冲突，不能自动去重')
                item.update(status='duplicate', duplicate_of=previous_id)
                continue
            seen[identity] = event['event_id'], fingerprint
            if event['event_id'] in blind_members:
                item.update(status='planned', asset_version=blind_members[event['event_id']])
                continue
            try:
                blind = bool(gift.get('blind_gift'))
                guard = gift.get('event_type') == 'GUARD_BUY'
                binding = guard_event(session, event['event_id'], include_recorded_tail=include_tail) if guard else None
                if catalog is not None and not guard and not blind:
                    select_effect(catalog, event['gift_id'], event['count'])
                asset_path = None
                if not blind:
                    asset_path, _ = cached_asset(args.cache, config['room_id'], event['gift_id'], event['count'],
                                                  binding['effect_id'] if binding else None)
                plan = gift_render.make_plan(Namespace(
                    asset=asset_path, base=None, session=session, at=None if guard or blind else event['at'],
                    event_id=event['event_id'] if guard or blind else None, room=None, gift_id=None,
                    count=1, cache=args.cache, font=args.font, username=None, before=before, after=after,
                    include_recorded_tail=include_tail,
                    canvas=args.canvas, base_display_aspect=aspects.get(event['event_id']),
                    region=regions.get(event['event_id']), placement='center-fit'))
                at = (datetime.fromisoformat(event['at'].replace('Z', '+00:00')) -
                      datetime.fromisoformat(config['start_utc'].replace('Z', '+00:00'))).total_seconds()
                # 使用场次相对秒量化，避免大epoch浮点数影响相接窗口判断。
                start = math.floor((at-before)*30+1e-7)/30
                end = math.ceil((at+plan['overlay']['duration']+after)*30-1e-7)/30
                if origin+end > scope['recorded_tail']['recorded_end']+1e-6:
                    raise ValueError('完整动画及前后文超出同一连续录制尾段，不能跨场取片或截短')
                members = [event['event_id']]
                if blind:
                    group = plan['dynamic']['resources']['binding']
                    members = group['event_ids']
                    if members[0] != event['event_id']:
                        raise ValueError('盲盒主事件顺序不符')
                    if any(m not in {e['event_id'] for e in enumerated} for m in members):
                        raise ValueError('盲盒结果未完整落在场次候选中')
                    if catalog is not None:
                        from .gift_blind import check_result_effects
                        check_result_effects(group, catalog)
                    for member in members:
                        blind_members[member] = plan['asset_version']
                windows.append({'event_id': event['event_id'], 'event_ids': members, 'start': start, 'end': end,
                                'at': at, 'plan': plan, 'aspect': aspects.get(event['event_id'])})
                item.update(status='planned', asset_version=plan['asset_version'])
            except (FileNotFoundError, ValueError, KeyError) as error:
                item.update(status='uncovered', reason=str(error))
    scenes = []
    for group in merge_windows(windows, args.gap):
        length = number(group['end']-group['start'], minimum=.01)
        aspect_values = {e['aspect'] for e in group['events'] if e['aspect'] is not None}
        if len(aspect_values) > 1:
            raise ValueError('重叠场景有不同的人工比例覆盖，须先核对素材')
        start_utc = datetime.fromtimestamp(origin+group['start'], timezone.utc).isoformat()
        clips = source_clips(session=session, at=start_utc, before=0, duration=length,
                             display_aspect=next(iter(aspect_values), None))
        effects = []
        for event in sorted(group['events'], key=lambda e: (e['at'], e['event_id'])):
            spec = {key: event['plan'].get(key) for key in EFFECT_KEYS}
            spec.update(event_id=event['event_id'], event_ids=event['event_ids'],
                        overlay={**event['plan']['overlay'], 'at': event['at']-group['start']})
            effects.append(spec)
        scene = {**group['events'][0]['plan'], **effects[0], 'clips': clips,
                 'canvas': list(canvas), 'duration': length, 'source_at': start_utc,
                 'source_kind': 'session', 'overlays': effects,
                 'source_start': group['start'], 'source_end': group['end']}
        scenes.append(scene)
    manifest_path = session/'offline/manifest.json'
    result = {'batch_schema_version': 1, 'session': str(session), 'canvas': list(canvas), 'fps': 30,
              'manifest': {'path': str(manifest_path), 'sha256': sha_file(manifest_path)},
              'inventory': entries, 'scenes': scenes, 'historical_web_playback': False,
              'recorded_tail': scope['recorded_tail'],
              'playback_policy': 'event_timestamp_overlay_no_web_queue_emulation',
              'naive_clip_seconds': sum(w['plan']['duration'] for w in windows),
              'duration': sum(scene['duration'] for scene in scenes)}
    validate_batch(result)
    return result


def validate_batch(plan):
    if plan.get('batch_schema_version') != 1 or plan.get('fps') != 30:
        raise ValueError('不支持的批次计划')
    dimensions('x'.join(str(n) for n in plan['canvas']))
    manifest = plan['manifest']
    if (Path(manifest['path']).resolve() != Path(plan['session']).resolve()/'offline/manifest.json'
            or sha_file(manifest['path']) != manifest['sha256']):
        raise ValueError('批次来源清单已改变或不属于所选场次')
    rows = json.loads(Path(manifest['path']).read_text())['rows']
    if 'recorded_tail' in plan:
        from .gift_scope import recorded_tail
        tail = plan['recorded_tail']
        if any(tail[k] != value for k, value in recorded_tail(plan['session']).items()):
            raise ValueError('录像尾段范围与冻结清单不符')
        tail_ids = [e['event_id'] for e in tail['events']]
        selected_tail = [e['event_id'] for e in plan['inventory'] if absolute_time(e['at']) >= tail['cutoff']]
        if (len(tail_ids) != len(set(tail_ids)) or
                selected_tail != (tail_ids if tail['included'] else []) or
                any(not tail['cutoff'] <= absolute_time(e['at']) < tail['recorded_end'] for e in tail['events'])):
            raise ValueError('尾段礼物未按显式选择完整列入清单')
    by_id = {e['event_id']: e for e in plan['inventory']}
    if len(by_id) != len(plan['inventory']):
        raise ValueError('批次覆盖清单的行ID重复')
    expected = [e['event_id'] for e in plan['inventory'] if e['status'] == 'planned']
    actual, previous_end, previous_wall_end = [], None, None
    used_ranges = {}
    if not plan['scenes'] and expected:
        raise ValueError('批次没有可渲染场景；请核对素材缓存与覆盖清单')
    for scene in plan['scenes']:
        wall_start = absolute_time(scene['source_at'])
        if ('recorded_tail' in plan and
                wall_start+scene['duration'] > plan['recorded_tail']['recorded_end']+1e-6):
            raise ValueError('场景取片超出同一连续录制尾段')
        if (scene['canvas'] != plan['canvas'] or scene['source_end'] <= scene['source_start']
                or abs(scene['duration']-(scene['source_end']-scene['source_start'])) > .00001
                or (previous_end is not None and scene['source_start'] < previous_end-1e-7)
                or (previous_wall_end is not None and wall_start < previous_wall_end-.00001)):
            raise ValueError('批次场景重叠、倒序或规格不一致')
        previous_end = scene['source_end']
        previous_wall_end = wall_start+scene['duration']
        selected = select_rows(rows, wall_start, previous_wall_end)
        if len(selected) != len(scene['clips']):
            raise ValueError('实际取片未连续覆盖声明的场景时间')
        for clip, (row, offset, length) in zip(scene['clips'], selected, strict=True):
            source = Path(row['path']).resolve()
            if source.is_file():
                if file_stat(source) != {key:row[key] for key in ('size', 'mtime_ns')}:
                    raise ValueError('批次清单原片已改变')
            else:
                source = archived_input(row).resolve()
            if (Path(clip['path']).resolve() != source or abs(clip['start']-offset) > .00001
                    or abs(clip['duration']-length) > .00001):
                raise ValueError('实际取片路径或偏移与场景墙钟映射不符')
            spans = used_ranges.setdefault(str(source), [])
            left, right = clip['start'], clip['start']+clip['duration']
            if any(min(right, b)-max(left, a) > .00001 for a,b in spans):
                raise ValueError('实际来源区间被重复播放')
            spans.append((left, right))
        if not isinstance(scene.get('overlays'), list) or not 1 <= len(scene['overlays']) <= 256:
            raise ValueError('每个批次场景须包含1至256个特效')
        for effect in scene['overlays']:
            members = effect.get('event_ids', [effect['event_id']])
            dynamic = effect.get('dynamic') or {}
            expected_members = (dynamic['resources']['binding']['event_ids'] if dynamic.get('kind') == 'blind'
                                else [effect['event_id']])
            if members != expected_members or effect['event_id'] != members[0]:
                raise ValueError('特效与原始批次的结果覆盖不符')
            actual.extend(members)
            entry = by_id.get(effect['event_id'])
            if entry is None:
                raise ValueError('特效引用了不存在的事件')
            relative = (datetime.fromisoformat(entry['at'].replace('Z', '+00:00')) -
                        datetime.fromisoformat(scene['source_at'].replace('Z', '+00:00'))).total_seconds()
            if abs(relative-effect['overlay']['at']) > .00001:
                raise ValueError('特效触发时间与事件时间不符')
            gift_render.validate_plan({**scene, **effect})
    if len(expected) != len(set(expected)) or sorted(actual) != sorted(expected):
        raise ValueError('批次特效事件必须恰好覆盖每条去重后的可渲染事件一次')
    if abs(sum(s['duration'] for s in plan['scenes'])-plan['duration']) > .00001:
        raise ValueError('批次总长不符')


def render_batch(plan, output):
    validate_batch(plan)
    if not plan['scenes']:
        raise ValueError('批次没有可渲染场景；未覆盖原因见计划inventory')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'batch-plan.json', plan)
    command = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-n']
    graph, concat, chapters, cursor = [], [], [';FFMETADATA1'], 0.0
    for index, scene in enumerate(plan['scenes']):
        directory = output/f'scene-{index:04d}'
        gift_render.render(scene, directory)
        command += ['-threads', '2', '-i', str(directory/'video.mp4')]
        duration = scene['duration']
        graph += [f'[{index}:v]trim=duration={duration},setpts=PTS-STARTPTS[v{index}]',
                  f'[{index}:a]aresample=48000,atrim=duration={duration},'
                  f'asetpts=PTS-STARTPTS[a{index}]']
        concat.append(f'[v{index}][a{index}]')
        chapters += ['[CHAPTER]', 'TIMEBASE=1/1000', f'START={round(cursor*1000)}',
                     f'END={round((cursor+duration)*1000)}', f'title=scene-{index+1}']
        cursor += duration
    metadata = output/'chapters.ffmetadata'
    metadata.write_text('\n'.join(chapters)+'\n')
    count = len(plan['scenes'])
    command += ['-i', str(metadata)]
    graph += [''.join(concat)+f'concat=n={count}:v=1:a=1[vc][a]',
              f'[vc]tpad=stop_mode=clone:stop_duration=1,fps=30:start_time=0,trim=duration={plan["duration"]}[v]']
    run(command+['-filter_complex_threads', '2', '-filter_complex', ';'.join(graph), '-map', '[v]', '-map', '[a]',
                 '-map_metadata', str(count), '-map_chapters', str(count),
                 '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
                 '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(output/'video.mp4')])
    return verify_batch(output)


def verify_batch(output):
    output = Path(output).resolve(strict=True)
    plan = json.loads((output/'batch-plan.json').read_text())
    validate_batch(plan)
    video = output/'video.mp4'
    metadata = probe(video)
    v = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
    if ([v['width'], v['height']] != plan['canvas'] or v.get('sample_aspect_ratio') != '1:1'
            or v.get('avg_frame_rate') != '30/1'
            or abs(float(metadata['format']['duration'])-plan['duration']) > .15
            or int(v.get('nb_frames', 0)) != round(plan['duration']*30)
            or sum(s['codec_type'] == 'audio' for s in metadata['streams']) != 1):
        raise ValueError('批次成片画幅、时长、帧率、帧数或音轨验收失败')
    run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(video),
         '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'])
    for scene in plan['scenes']:
        if any(file_stat(c['path']) != c['stat'] for c in scene['clips']):
            raise ValueError('批次来源文件已改变')
    coverage = [{**entry, 'status': 'rendered' if entry['status']=='planned' else entry['status']}
                for entry in plan['inventory']]
    write_json(output/'coverage.json', coverage)
    result = {'verified': True, 'video': str(video), 'duration': float(metadata['format']['duration']),
              'scenes': len(plan['scenes']), 'rendered_events': sum(e['status']=='rendered' for e in coverage),
              'duplicate_events': sum(e['status']=='duplicate' for e in coverage),
              'uncovered_events': sum(e['status'] not in ('rendered', 'duplicate') for e in coverage),
              'naive_clip_seconds': plan['naive_clip_seconds'], 'source_seconds': plan['duration'],
              'sha256': sha_file(video), 'visual_review_required': True}
    if 'recorded_tail' in plan:
        result['recorded_tail'] = plan['recorded_tail']
    write_json(output/'verification.json', result)
    return result


def configure_parser(actions):
    item = actions.add_parser('batch-plan', help='消息身份去重、合并重叠录像窗口；离线读取既有缓存')
    item.add_argument('--session', type=Path, required=True)
    item.add_argument('--cache', type=Path, default=cache_default())
    item.add_argument('--catalog', type=Path, help='可选目录快照，用于解释未覆盖原因，不下载素材')
    item.add_argument('--font', type=Path)
    item.add_argument('--canvas', default='1920x1080')
    item.add_argument('--before', type=float, default=6)
    item.add_argument('--after', type=float, default=6)
    item.add_argument('--gap', type=float, default=0)
    item.add_argument('--include-recorded-tail', action='store_true',
                      help='显式纳入cutoff后同一连续录制内的礼物；默认仅在recorded_tail报告尾段候选')
    item.add_argument('--event-region', action='append', default=[], metavar='ID=x,y,w,h')
    item.add_argument('--event-aspect', action='append', default=[], metavar='ID=ratio')
    item = actions.add_parser('batch-render')
    item.add_argument('--plan', type=Path, required=True)
    item.add_argument('--output', type=Path, required=True)
    item = actions.add_parser('batch-verify')
    item.add_argument('--output', type=Path, required=True)
