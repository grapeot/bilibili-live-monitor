"""单场看板的无外发初始化、来源登记、本地重建与验收入口。"""
import html
import json
import math
import re
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .live_share import (
    atomic_json,
    epoch,
    fingerprint,
    followup_candidates,
    iso,
    make_snapshot,
    read_source,
    render_snapshot,
    scope_source,
)
from .song_sources import register_source, verified_titles


def validate_config(config):
    for name in ('workspace', 'database', 'room_id', 'label', 'start_utc', 'live_start'):
        if not isinstance(config.get(name), str) or not config[name]:
            raise ValueError(f'缺少有效配置字段：{name}')
    if not config['room_id'].isdigit():
        raise ValueError('房间号必须是规范数字ID')
    start, end = epoch(config['start_utc']), config.get('deadline')
    if type(end) not in (int, float) or not math.isfinite(end) or not start < end <= start + 43200:
        raise ValueError('截止时间必须在开播后12小时以内')
    local_start = datetime.fromtimestamp(start, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
    if config['live_start'] != local_start:
        raise ValueError('live_start须为start_utc对应的北京时间')
    for name in ('workspace', 'database'):
        if not Path(config[name]).is_absolute():
            raise ValueError(f'{name}必须为绝对路径')
    public = Path(config['workspace']).resolve() / 'public'
    if Path(config['database']).resolve().is_relative_to(public):
        raise ValueError('原始数据库不能放在public目录')
    if config.get('ai_enabled', bool(config.get('ai_session'))):
        for name in ('ai_session', 'model', 'opencode_cwd', 'opencode_python'):
            if not isinstance(config.get(name), str) or not config[name]:
                raise ValueError(f'启用AI须配置{name}')
        if '/' not in config['model']:
            raise ValueError('模型必须显式包含provider/model')
    if bool(config.get('remote')) != bool(config.get('url')):
        raise ValueError('发布目标remote与url必须一起配置')
    if config.get('remote') and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*:/[^\r\n]+/', config['remote']):
        raise ValueError('remote须为HOST:/绝对目录/形式')
    if config.get('url') and not config['url'].startswith('https://'):
        raise ValueError('公开地址须使用HTTPS')
    rules = config.get('followup_rules', {})
    if not isinstance(rules, dict):
        raise ValueError('followup_rules必须为对象')
    for name in ('lamp_ids', 'blind_gift_names'):
        if name in rules and (not isinstance(rules[name], list)
                              or any(not isinstance(x, str) or not x for x in rules[name])):
            raise ValueError(f'{name}必须为非空字符串列表（可以为空列表）')
    for name in ('include_blind', 'exclude_guard_lamps'):
        if name in rules and type(rules[name]) is not bool:
            raise ValueError(f'{name}必须为布尔值')
    return config


def load_config(path):
    config = validate_config(json.loads(Path(path).read_text()))
    return {**config, '_config_path': str(Path(path).resolve())}


def initialize(args, *, resume=False):
    workspace = args.workspace.resolve()
    database = args.database.resolve()
    if not database.is_file():
        raise ValueError('源数据库不存在；初始化不会创建或启动采集')
    start = epoch(args.start)
    if not math.isfinite(args.hours) or not 0 < args.hours <= 12:
        raise ValueError('hours必须大于0且不超过12')
    ai_values = [args.ai_session, args.model, args.opencode_cwd, args.opencode_python]
    if any(ai_values) and not all(ai_values):
        raise ValueError('AI四个参数必须同时提供，或全部不提供')
    config = {'workspace': str(workspace), 'database': str(database), 'room_id': args.room,
              'label': args.label, 'start_utc': iso(start),
              'live_start': datetime.fromtimestamp(start, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'),
              'deadline': start + args.hours * 3600, 'api': args.api, 'launcher': args.launcher,
              'job_prefix': 'live_share_' + args.room + '_' + secrets.token_hex(4),
              'ai_enabled': bool(args.ai_session), 'ai_session': args.ai_session, 'model': args.model,
              'opencode_cwd': str(args.opencode_cwd.resolve()) if args.opencode_cwd else None,
              # venv interpreters are symlinks: resolving one discards its installed packages.
              'opencode_python': str(args.opencode_python.expanduser().absolute()) if args.opencode_python else None,
              'remote': args.remote, 'url': args.url, 'approved_songs': [], 'followup_overrides': {},
              'followup_rules': {'include_blind': not args.no_blind_gifts,
                                 'blind_gift_names': args.blind_gift_name,
                                 'exclude_guard_lamps': not args.include_guard_lamps}}
    validate_config(config)
    if workspace.exists():
        if not workspace.is_dir():
            raise ValueError('工作目录不是目录')
        allowed = {'config.json', 'annotations.json', 'verified_songs.json'}
        allowed |= {name + '.tmp' for name in allowed}
        if any(not resume or p.name not in allowed or not p.is_file() or p.is_symlink()
               for p in workspace.iterdir()):
            raise ValueError('工作目录非空；必须为每场直播创建新目录')
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(workspace / 'config.json', config)
    if not (workspace / 'annotations.json').exists():
        atomic_json(workspace / 'annotations.json', {'reviews': [], 'completed_followups': [], 'song_requests': []})
    if not (workspace / 'verified_songs.json').exists():
        atomic_json(workspace / 'verified_songs.json', [])
    return {'config': str(workspace / 'config.json'), 'ai_enabled': config['ai_enabled'],
            'published': False, 'started': False}


def source_at_cutoff(config):
    workspace = Path(config['workspace'])
    state = json.loads((workspace / 'state.json').read_text())
    cutoff = min(state.get('cutoff', state.get('last_live_at', epoch(config['start_utc']))), config['deadline'])
    if cutoff < epoch(config['start_utc']):
        raise ValueError('冻结上界早于本场开播')
    source = scope_source(read_session_source(config),
                          config['start_utc'], cutoff)
    return state, cutoff, source


def read_session_source(config):
    source = read_source(Path(config['database']), config['room_id'], config['start_utc'])
    if config.get('offline_segments'):
        path = Path(config['offline_segments']).resolve()
        if not path.is_relative_to(Path(config['workspace']).resolve()) or 'public' in path.parts:
            raise ValueError('离线转写须保存在本场私有工作区')
        source['segments'] = json.loads(path.read_text())
    else:
        path = Path(config['workspace']) / 'live_backfill_segments.json'
        if path.exists():
            if path.is_symlink():
                raise ValueError('直播回填须保存在本场私有工作区')
            offline = json.loads(path.read_text())
            source['segments'] = prefer_offline(source['segments'], offline)
    return source


def prefer_offline(live, offline):
    """只替换已完整覆盖的实时窗口，保留缺口及跨回填边界的实时证据。"""
    spans = []
    for row in sorted(offline, key=lambda s: epoch(s['start_utc'])):
        start, end = epoch(row['start_utc']), epoch(row['end_utc'])
        if end < start:
            raise ValueError('离线窗口结束早于开始')
        if spans and start <= spans[-1][1] + .001:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    retained = [row for row in live if not any(
        start - .001 <= epoch(row['start_utc']) and epoch(row['end_utc']) <= end + .001
        for start, end in spans)]
    return sorted([*offline, *retained], key=lambda row: (epoch(row['start_utc']), row['seq']))


def export_snapshot(config):
    workspace = Path(config['workspace'])
    state, cutoff, source = source_at_cutoff(config)
    annotations = json.loads((workspace / 'annotations.json').read_text())
    completed = state.get('phase') == 'completed' or state.get('stop_intent')
    health = dict(state.get('last_health', {}))
    if completed:
        health['recording'] = False
    snapshot = make_snapshot(source, start=config['start_utc'], now=iso(cutoff), label=config['label'],
        phase='completed' if completed else 'live', health=health, annotations=annotations,
        approved_songs=tuple(config.get('approved_songs', [])) + verified_titles(workspace),
        followup_overrides=config.get('followup_overrides', {}), followup_rules=config.get('followup_rules'))
    destination = workspace / 'public'
    destination.mkdir(exist_ok=True)
    temporary = destination / 'index.html.tmp'
    temporary.write_text(render_snapshot(snapshot, navigation=workspace / 'navigation.json'))
    temporary.replace(destination / 'index.html')
    atomic_json(destination / 'snapshot.json', snapshot)
    return {'output': str(destination), 'published': False, 'phase': snapshot['phase'], 'cutoff': iso(cutoff)}


def verify_snapshot(config):
    workspace = Path(config['workspace'])
    public = workspace / 'public'
    snapshot = json.loads((public / 'snapshot.json').read_text())
    page = (public / 'index.html').read_text()
    errors = []
    forbidden = {'uid', 'sender_uid', 'text', 'evidence', 'fingerprint', 'source_key',
                  'reviews', 'song_requests', 'database', 'workspace', 'opencode_python',
                  'corrected_text', 'correction_id', 'correction_reason', 'correction_evidence'}
    def check(value):
        if isinstance(value, dict):
            errors.extend('private_field:' + key for key in value if key in forbidden)
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)
    check(snapshot)
    if snapshot.get('session_start') != config['start_utc'] or snapshot.get('label') != config['label']:
        errors.append('snapshot_identity_mismatch')
    if set(snapshot.get('series', {})) != {'entries', 'online', 'fans', 'gifts'} or page.count('data-chart=') != 4:
        errors.append('expected_four_charts')
    state, cutoff, source = source_at_cutoff(config)
    if epoch(snapshot['updated_at']) != cutoff:
        errors.append('stale_or_incorrect_cutoff')
    stopped_phases = {'completed', 'finalizing'} if config.get('workflow_key') else {'completed'}
    if (state.get('phase') == 'completed' or state.get('stop_intent')) and snapshot.get('phase') not in stopped_phases:
        errors.append('completed_page_still_live')
    known = set(config.get('approved_songs', [])) | set(verified_titles(workspace))
    annotations = json.loads((workspace / 'annotations.json').read_text())
    current = {s['seq']: fingerprint(s) for s in source['segments'] if s.get('is_final') and s.get('text')}
    reviews = {r.get('seq'): r for r in annotations.get('reviews', []) if isinstance(r, dict)
               and r.get('fingerprint') == current.get(r.get('seq')) and r.get('seq') in current}
    unpublished = sorted({r['song'] for r in reviews.values() if isinstance(r.get('song'), str)
                          and r['song'] not in known})
    candidate_ids = {row['id'] for row in followup_candidates(source, config['start_utc'],
                                                             config.get('followup_rules'))
                     if config.get('followup_overrides', {}).get(row['uid'], {}).get('status') != 'excluded'}
    requests = annotations.get('song_requests', [])
    request_titles = {row['song'] for row in (requests if isinstance(requests, list) else [])
                      if isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] in candidate_ids
                      and isinstance(row.get('song'), str) and row['song'] and row['song'] not in known}
    unpublished = sorted(set(unpublished) | request_titles)
    for row in snapshot.get('activity_summary', []):
        if row.get('title') and html.escape(row['title'], quote=True) not in page:
            errors.append('title_missing_from_html')
    return {'valid': not errors, 'errors': errors, 'unpublished_songs': unpublished,
            'unreviewed_windows': len(current.keys() - reviews.keys()),
            'unnamed_singing_windows': sum(r.get('label') == 'singing' and not r.get('song') for r in reviews.values()),
            'phase': snapshot.get('phase'), 'cutoff': snapshot['updated_at'], 'remote_verified': False}


def configure_parser(commands):
    parser = commands.add_parser('live-share', help='单场实时看板：初始化、来源登记、本地重建与验收')
    sub = parser.add_subparsers(dest='live_share_command', required=True)
    init = sub.add_parser('init', help='仅创建私有配置；不启动、不发布、不调用AI')
    init.add_argument('--workspace', type=Path, required=True)
    init.add_argument('--database', type=Path, required=True)
    init.add_argument('--room', required=True)
    init.add_argument('--label', required=True)
    init.add_argument('--start', required=True, help='带时区的本场实际开播时间')
    init.add_argument('--hours', type=float, default=12)
    init.add_argument('--api', default='http://127.0.0.1:8765')
    init.add_argument('--launcher', default='http://127.0.0.1:7997')
    for flag in ('ai-session', 'model', 'remote', 'url'):
        init.add_argument('--' + flag)
    for flag in ('opencode-cwd', 'opencode-python'):
        init.add_argument('--' + flag, type=Path)
    init.add_argument('--blind-gift-name', action='append', default=[])
    init.add_argument('--no-blind-gifts', action='store_true')
    init.add_argument('--include-guard-lamps', action='store_true')
    for command in ('verify', 'export', 'register-song'):
        child = sub.add_parser(command)
        child.add_argument('--config', type=Path, required=True)
        if command == 'register-song':
            child.add_argument('--title', required=True)
            child.add_argument('--source', type=Path, required=True)
            child.add_argument('--result-index', type=int, required=True)
    return parser


def main(args):
    try:
        if args.live_share_command == 'init':
            result = initialize(args)
        else:
            config = load_config(args.config)
            if args.live_share_command == 'register-song':
                result = register_source(Path(config['workspace']), args.title, args.source, args.result_index)
            elif args.live_share_command == 'export':
                result = export_snapshot(config)
            else:
                result = verify_snapshot(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('valid', True) else 1
    except (ValueError, TypeError, KeyError, IndexError, OSError) as error:
        print(json.dumps({'error': f'{type(error).__name__}: {error}'}, ensure_ascii=False))
        return 1
