"""按冻结计划离线叠加单个礼物；场次事件供skill按需批量编排。"""
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from .archive_media import file_stat
from .gift_assets import cache_default, cached_asset, load_asset, sha_file, validate_config, write_json
from .gift_dynamic import BLIND_TAG, render_rgba, text_images, validate_dynamic
from .gift_event import guard_event, is_blind_event
from .gift_media import absolute_time, aspect, dimensions, number, prepare_base, session_config, source_clips
from .trailer import probe, run


def events(session, *, include_recorded_tail=False):
    root, config = session_config(session)
    start = absolute_time(config['start_utc'])
    manifest_path = root / 'offline/manifest.json'
    cutoff = (json.loads(manifest_path.read_text())['cutoff'] if manifest_path.exists()
              else datetime.now(timezone.utc).timestamp())
    from .gift_scope import recorded_tail
    tail = (recorded_tail(session) if manifest_path.exists() else
            {'cutoff': cutoff, 'recorded_end': cutoff, 'stop_reason': 'no_manifest'})
    database = Path(config['database']).resolve(strict=True)
    lower = datetime.fromtimestamp(start, timezone.utc).isoformat()
    upper = datetime.fromtimestamp(tail['recorded_end'], timezone.utc).isoformat()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        rows = db.execute('SELECT id,at_utc,data FROM timeline WHERE room_id=? AND kind=? AND at_utc>=? '
                          'AND at_utc<? ORDER BY at_utc,id',
                          (str(config['room_id']), 'gift', lower, upper)).fetchall()
    result = []
    for ident, at, data in rows:
        if not start <= absolute_time(at) < tail['recorded_end']:
            continue
        value = json.loads(data).get('gift', {})
        gift_id = value.get('gift_id')
        result.append({'event_id': ident, 'at': at, 'gift_id': gift_id, 'count': value.get('num', 1),
                       'gift_name': value.get('gift_name'),
                       'status': 'candidate' if gift_id else 'no_gift_id'})
    tail_events = [e for e in result if absolute_time(e['at']) >= cutoff]
    return {'room_id': config['room_id'], 'session': str(root), 'cutoff': cutoff,
            'events': result if include_recorded_tail else [e for e in result if absolute_time(e['at']) < cutoff],
            'recorded_tail': {**tail, 'included': bool(include_recorded_tail), 'events': tail_events}}


def region(value, canvas):
    rect = [0, 0, *canvas] if value is None else [int(n) for n in value.split(',')]
    if (len(rect) != 4 or min(rect[:2]) < 0 or min(rect[2:]) < 2
            or rect[0] + rect[2] > canvas[0] or rect[1] + rect[3] > canvas[1]):
        raise ValueError('特效区域须为画布内的x,y,w,h')
    return rect


def make_plan(args):
    binding = None
    at, room, gift_id, count = (args.at, getattr(args, 'room', None),
                               getattr(args, 'gift_id', None), getattr(args, 'count', 1))
    if getattr(args, 'event_id', None) is not None:
        if not args.session or at is not None or room or gift_id or count != 1 or getattr(args, 'username', None):
            raise ValueError('--event-id须与--session使用，且不混用时间、礼物、数量或昵称覆盖')
        if is_blind_event(args.session, args.event_id):
            from .gift_blind import bind_event
            binding = bind_event(args.session, args.event_id,
                                 include_recorded_tail=getattr(args, 'include_recorded_tail', False))
        else:
            binding = guard_event(args.session, args.event_id,
                                  include_recorded_tail=getattr(args, 'include_recorded_tail', False))
        at, room, gift_id, count = (binding[k] for k in ('at', 'room_id', 'gift_id', 'count'))
    if at is None:
        raise ValueError('请提供--at，或--session与--event-id')
    if args.asset:
        asset_path = Path(args.asset).resolve(strict=True)
        asset = load_asset(asset_path)
    else:
        if args.session:
            _, config = session_config(args.session)
            room = config['room_id']
        if not room or not gift_id:
            raise ValueError('请提供--asset，或--room/--gift-id（场次自动提供房间）')
        try:
            asset_path, asset = cached_asset(args.cache, room, gift_id, count,
                                              binding.get('effect_id') if binding else None)
        except FileNotFoundError as error:
            raise ValueError('礼物未缓存；先运行gift-assets fetch。render/plan均不联网') from error
    if binding and any(asset[k] != binding[k] for k in ('room_id', 'gift_id', 'count', 'effect_id') if k in binding):
        raise ValueError('素材不属于所选续舰事件')
    before, after = number(args.before), number(args.after)
    config = json.loads(Path(asset['config']['path']).read_text())
    info = validate_config(config)
    dynamic = None
    sources = validate_dynamic(config)
    if binding and 'results' in binding and (len(sources) != 1 or sources[0]['srcTag'] != BLIND_TAG):
        raise ValueError('盲盒素材缺少唯一的结果图片槽，不能省略结果卡')
    if sources and sources[0]['srcTag'] == BLIND_TAG:
        from .gift_blind import card_images, load_resources
        if not binding or 'results' not in binding:
            raise ValueError('盲盒图片槽须通过--session与--event-id绑定原始结果')
        resources = load_resources(args.cache, binding)
        card_images(config, resources, asset)
        dynamic = {'kind': 'blind', 'resources': resources}
    elif sources:
        font_path = getattr(args, 'font', None)
        if not font_path:
            raise ValueError('动态文字须显式提供--font；文件模式还须--username')
        font_path = Path(font_path).resolve(strict=True)
        username = binding['username'] if binding else getattr(args, 'username', None)
        text_images(config, username, font_path)
        dynamic = {'username': username, 'font': {'path': str(font_path), 'sha256': sha_file(font_path)}}
    media = probe(Path(asset['media']['path']))
    videos = [s for s in media['streams'] if s['codec_type'] == 'video']
    if len(videos) != 1:
        raise ValueError('特效需要单路视频')
    validate_config(config, (videos[0]['width'], videos[0]['height']))
    if abs(float(media['format']['duration']) - info['f'] / info['fps']) > .2:
        raise ValueError('特效时长与配套JSON不符')
    effect_duration = info['f'] / info['fps']
    duration = number(before + effect_duration + after)
    tail_scope = None
    if getattr(args, 'include_recorded_tail', False):
        if not args.session:
            raise ValueError('--include-recorded-tail须与--session一起使用')
        from .gift_scope import recorded_tail
        root, _ = session_config(args.session)
        tail_scope = {**recorded_tail(root), 'session': str(root),
                      'manifest_sha256': sha_file(root/'offline/manifest.json')}
        if absolute_time(at)-before+duration > tail_scope['recorded_end']+1e-6:
            raise ValueError('完整动画及前后文超出同一连续录制尾段，不能跨场取片或截短')
    clips = source_clips(base=args.base, session=args.session, at=at, before=before,
                         duration=duration, display_aspect=args.base_display_aspect)
    canvas = dimensions(args.canvas)
    if canvas is None:
        ratio = aspect(clips[0]['display_aspect'])
        height = clips[0]['decoded_size'][1]
        canvas = (round(float(ratio) * height / 2) * 2, height)
        dimensions(f'{canvas[0]}x{canvas[1]}')
    plan = {'schema_version': 1, 'clips': clips, 'canvas': list(canvas),
            'duration': duration, 'fps': 30, 'asset_path': str(asset_path),
            'asset_sha256': sha_file(asset_path), 'asset_version': asset['version'],
            'gift_id': asset['gift_id'], 'count': asset['count'],
            'overlay': {'at': before, 'duration': effect_duration,
                        'placement': args.placement, 'region': region(args.region, canvas)},
            'source_at': at, 'source_kind': 'session' if args.session else 'file',
            'effect_audio': 'omitted', 'historical_web_playback': False}
    if binding and 'results' not in binding:
        plan['event_binding'] = binding
    if dynamic:
        plan['dynamic'] = dynamic
    if tail_scope:
        plan['recorded_tail_scope'] = tail_scope
    return plan


def validate_plan(plan):
    if plan.get('schema_version') != 1 or plan.get('fps') != 30:
        raise ValueError('不支持的渲染计划版本或帧率')
    duration = number(plan['duration'], minimum=.01)
    if 'recorded_tail_scope' in plan:
        from .gift_scope import recorded_tail
        scope = plan['recorded_tail_scope']
        root = Path(scope['session'])
        if (sha_file(root/'offline/manifest.json') != scope['manifest_sha256'] or
                any(scope[k] != value for k, value in recorded_tail(root).items())):
            raise ValueError('单事件尾段范围与冻结录像清单不符')
        wall_start = absolute_time(plan['source_at'])
        if 'source_start' not in plan:
            wall_start -= plan['overlay']['at']
        if wall_start+duration > scope['recorded_end']+1e-6:
            raise ValueError('场景取片超出同一连续录制尾段，不能跨场取片')
    dimensions('x'.join(str(n) for n in plan['canvas']))
    overlay = plan['overlay']
    if overlay['placement'] not in ('center-fit', 'left-fit', 'right-fit'):
        raise ValueError('特效放置方式无效')
    region(','.join(str(n) for n in overlay['region']), plan['canvas'])
    at = number(overlay['at'])
    effect_length = number(overlay['duration'], minimum=.01)
    if at + effect_length > duration + .001:
        raise ValueError('特效超出输出时间线')
    asset_path = Path(plan['asset_path'])
    if sha_file(asset_path) != plan['asset_sha256']:
        raise ValueError('计划引用的礼物版本文件已改变')
    asset = load_asset(asset_path)
    if asset['version'] != plan['asset_version']:
        raise ValueError('计划引用的礼物版本不符')
    config = json.loads(Path(asset['config']['path']).read_text())
    info = validate_config(config)
    dynamic = plan.get('dynamic')
    if validate_dynamic(config):
        if not isinstance(dynamic, dict):
            raise ValueError('计划缺少动态文字绑定')
        if dynamic.get('kind') == 'blind':
            from .gift_blind import card_images
            resources = dynamic['resources']
            binding = resources['binding']
            if any(binding[k] != asset[k] for k in ('room_id', 'gift_id', 'count')):
                raise ValueError('盲盒素材与原始批次不符')
            card_images(config, resources, asset)
        else:
            font = dynamic['font']
            if sha_file(font['path']) != font['sha256']:
                raise ValueError('计划引用的字体已改变')
            text_images(config, dynamic['username'], font['path'])
    elif dynamic:
        raise ValueError('静态素材不能包含动态文字绑定')
    if plan.get('event_binding'):
        binding = plan['event_binding']
        if any(binding[k] != asset[k] for k in ('room_id', 'gift_id', 'count', 'effect_id')):
            raise ValueError('计划事件与素材不符')
        if dynamic and dynamic['username'] != binding['username']:
            raise ValueError('计划昵称与事件不符')
    if abs(effect_length - info['f'] / info['fps']) > .001:
        raise ValueError('计划特效时长与素材不符')
    if not 1 <= len(plan['clips']) <= 100:
        raise ValueError('底层片段数量无效')
    for clip in plan['clips']:
        number(clip['start'], maximum=10**9)
        number(clip['duration'], minimum=.001)
        aspect(clip['display_aspect'])
        if file_stat(clip['path']) != clip['stat']:
            raise ValueError('计划引用的底层素材已改变')
        receipt = clip.get('archive_receipt')
        if receipt and file_stat(receipt['path']) != receipt['stat']:
            raise ValueError('计划引用的归档回执已改变')
    if abs(sum(c['duration'] for c in plan['clips']) - duration) > .06:
        raise ValueError('底层片段总长与计划不符')
    return asset, info


def render(plan, output):
    specifications = plan.get('overlays', [None])
    if not isinstance(specifications, list) or not 1 <= len(specifications) <= 256:
        raise ValueError('同一场景须含1至256个特效')
    effects = []
    for spec in specifications:
        candidate = plan if spec is None else {**plan, **spec}
        asset, info = validate_plan(candidate)
        effects.append((candidate, asset, info))
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / 'plan.json', plan)
    width, height = plan['canvas']
    parts = []
    for index, clip in enumerate(plan['clips']):
        ratio = Fraction(clip['display_aspect'])
        # Establish square-pixel display geometry BEFORE fitting into the output canvas.
        fit_h = min(height, width / float(ratio))
        fit_w, fit_h = max(2, int(fit_h * float(ratio) / 2) * 2), max(2, int(fit_h / 2) * 2)
        filters = (f'scale={fit_w}:{fit_h}:flags=lanczos,setsar=1,'
                   f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30:start_time=0')
        part = output / f'part-{index:03d}.mkv'
        run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-ss', str(clip['start']),
             '-i', clip['path'], '-t', str(clip['duration']), '-map', '0:v:0', '-map', '0:a:0',
             '-vf', filters, '-af', 'aresample=48000,asetpts=PTS-STARTPTS',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-pix_fmt', 'yuv420p',
             '-c:a', 'pcm_s16le', '-ac', '2', str(part)])
        parts.append(part)
    # concat demuxer paths are generated relative names, never unescaped user paths.
    concat = output / 'parts.txt'
    concat.write_text(''.join(f"file '{part.name}'\n" for part in parts))
    base = output / 'base.mp4'
    run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'concat', '-safe', '1', '-i', str(concat),
         '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(base)])
    inputs, graph, intermediates = [], [], []
    previous = '[0:v]'
    for i, (candidate, asset, info) in enumerate(effects):
        assets = output / 'assets'
        if len(effects) > 1:
            assets = assets / f'overlay-{i:03d}'
        assets.mkdir(parents=True)
        for name, key in (('effect.mp4', 'media'), ('effect.json', 'config')):
            shutil.copyfile(asset[key]['path'], assets / name)
        write_json(assets / 'version.json', asset)
        effect_input = assets / 'effect.mp4'
        dynamic = candidate.get('dynamic')
        if dynamic:
            write_json(assets / 'bindings.json', dynamic)
            config = json.loads((assets / 'effect.json').read_text())
            if dynamic.get('kind') == 'blind':
                from .gift_blind import card_images
                images = card_images(config, dynamic['resources'])
                for ident, image in images.items():
                    image.save(assets / f'result-card-{ident}.png')
                effect_input = render_rgba(effect_input, config, None, None,
                                           output / f'effect-{i:03d}-rgba.mov', images=images)
            else:
                font = assets / ('font' + Path(dynamic['font']['path']).suffix)
                shutil.copyfile(dynamic['font']['path'], font)
                if sha_file(font) != dynamic['font']['sha256']:
                    raise ValueError('复制期间字体改变')
                effect_input = render_rgba(effect_input, config, dynamic['username'], font,
                                           output / f'effect-{i:03d}-rgba.mov')
            intermediates.append(effect_input)
            unpack = f'[{i+1}:v]'
        else:
            rx, ry, rw, rh = info['rgbFrame']
            ax, ay, aw, ah = info['aFrame']
            unpack = (f'[{i+1}:v]split=2[r{i}0][a{i}0];'
                      f'[r{i}0]crop={rw}:{rh}:{rx}:{ry},format=rgb24[r{i}];'
                      f'[a{i}0]crop={aw}:{ah}:{ax}:{ay},scale={rw}:{rh}:flags=bilinear,format=gray[a{i}];'
                      f'[r{i}][a{i}]alphamerge,')
        overlay = candidate['overlay']
        x, y, w, h = overlay['region']
        scale = min(w / info['w'], h / info['h'])
        ew, eh = (max(2, int(n*scale/2)*2) for n in (info['w'], info['h']))
        left = {'left-fit': x, 'right-fit': x+w-ew, 'center-fit': x+(w-ew)//2}[overlay['placement']]
        top = y+(h-eh)//2
        graph.append(unpack + f'scale={ew}:{eh}:flags=lanczos,setsar=1,'
                     f'setpts=PTS-STARTPTS+{overlay["at"]}/TB[g{i}];'
                     f'{previous}[g{i}]overlay={left}:{top}:eof_action=pass:repeatlast=0:format=auto[b{i}]')
        previous = f'[b{i}]'
        inputs += ['-i', str(effect_input)]
    graph.append(previous + f'tpad=stop_mode=clone:stop_duration=1,fps=30:start_time=0,'
                 f'trim=duration={plan["duration"]},format=yuv420p[v]')
    video = output / 'video.mp4'
    run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(base), *inputs,
         '-filter_complex_threads', '2', '-filter_complex', ';'.join(graph), '-map', '[v]', '-map', '0:a:0',
         '-t', str(plan['duration']), '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
         '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', str(video)])
    preview_at = effects[0][0]['overlay']['at'] + effects[0][0]['overlay']['duration'] / 2
    run(['ffmpeg', '-nostdin', '-v', 'error', '-ss', str(preview_at), '-i', str(base),
         '-ss', str(preview_at), '-i', str(video), '-filter_complex',
         '[0:v]scale=640:-2[l];[1:v]scale=640:-2[r];[l][r]hstack[v]',
         '-map', '[v]', '-frames:v', '1', '-update', '1', str(output / 'preview.jpg')])
    result = verify(output)
    # These are only this task's expendable normalized intermediates.
    for part in parts:
        part.unlink()
    concat.unlink()
    for intermediate in intermediates:
        intermediate.unlink()
    return result


def verify(output):
    output = Path(output).resolve(strict=True)
    plan = json.loads((output / 'plan.json').read_text())
    info = probe(output / 'video.mp4')
    streams = info['streams']
    video = next(s for s in streams if s['codec_type'] == 'video')
    if (len(streams) != 2 or not any(s['codec_type'] == 'audio' for s in streams)
            or [video['width'], video['height']] != plan['canvas']
            or video.get('sample_aspect_ratio') != '1:1'
            or abs(float(info['format']['duration']) - plan['duration']) > .15):
        raise ValueError('成片画幅、音轨或时长验收失败')
    run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-i', str(output / 'video.mp4'),
         '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'])
    result = {'verified': True, 'video': str(output / 'video.mp4'), 'canvas': plan['canvas'],
              'duration': float(info['format']['duration']), 'asset_version': plan['asset_version'],
              'source_files_unchanged': all(file_stat(c['path']) == c['stat'] for c in plan['clips']),
              'sha256': sha_file(output / 'video.mp4'), 'visual_review_required': True}
    if not result['source_files_unchanged']:
        raise ValueError('渲染期间来源发生变化')
    write_json(output / 'verification.json', result)
    return result


def configure_parser(commands):
    parser = commands.add_parser('gift-render', help='离线礼物动画合成与场次事件枚举')
    actions = parser.add_subparsers(dest='action', required=True)
    from .gift_batch import configure_parser as configure_batch
    configure_batch(actions)
    listing = actions.add_parser('events')
    listing.add_argument('--session', type=Path, required=True)
    check = actions.add_parser('verify')
    check.add_argument('--output', type=Path, required=True)
    prepare = actions.add_parser('prepare', help='在新的私有目录准备原始分片解码头；不写生产目录')
    prepare.add_argument('--base', type=Path, required=True)
    prepare.add_argument('--output', type=Path, required=True)
    for action in ('plan', 'render'):
        item = actions.add_parser(action)
        inputs = item.add_mutually_exclusive_group(required=True)
        inputs.add_argument('--base', type=Path)
        inputs.add_argument('--session', type=Path)
        if action == 'render':
            inputs.add_argument('--plan', type=Path)
            item.add_argument('--output', type=Path, required=True)
        item.add_argument('--at')
        item.add_argument('--event-id', help='与场次一起定位GUARD_BUY并绑定原始续舰消息')
        item.add_argument('--include-recorded-tail', action='store_true',
                          help='允许绑定cutoff后同一连续录制内的尾段事件')
        item.add_argument('--username', help='明确文件模式的动态昵称；事件模式从原始消息读取')
        item.add_argument('--font', type=Path, help='显式指定本机TTF/OTF/TTC字体，计划固定SHA256')
        item.add_argument('--asset', type=Path)
        item.add_argument('--cache', type=Path, default=cache_default())
        item.add_argument('--room')
        item.add_argument('--gift-id')
        item.add_argument('--count', type=int, default=1)
        item.add_argument('--before', type=float, default=6)
        item.add_argument('--after', type=float, default=6)
        item.add_argument('--base-display-aspect')
        item.add_argument('--canvas')
        item.add_argument('--placement', choices=('center-fit', 'left-fit', 'right-fit'), default='center-fit')
        item.add_argument('--region')


def main(args):
    try:
        if args.action in ('batch-plan', 'batch-render', 'batch-verify'):
            from .gift_batch import make_batch, render_batch, verify_batch
            if args.action == 'batch-plan':
                result = make_batch(args)
            elif args.action == 'batch-render':
                result = render_batch(json.loads(args.plan.read_text()), args.output)
            else:
                result = verify_batch(args.output)
        elif args.action == 'prepare':
            result = prepare_base(args.base, args.output)
        elif args.action == 'events':
            result = events(args.session)
        elif args.action == 'verify':
            result = verify(args.output)
        else:
            if getattr(args, 'plan', None):
                plan = json.loads(args.plan.read_text())
            else:
                if args.at is None and not getattr(args, 'event_id', None):
                    raise ValueError('请提供--at：本地文件为秒数，场次为带时区的时间')
                plan = make_plan(args)
            result = plan if args.action == 'plan' else render(plan, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(json.dumps({'error_type': type(error).__name__, 'error': str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
