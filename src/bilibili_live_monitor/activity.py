"""块级唱歌/讲话候选与人工复核；不把文本启发式当成音频分类真值。"""
import hashlib
import json
import math
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .activity_page import page_html
from .trends import aggregate, digest, instant

LABELS = ('singing', 'speaking', 'mixed', 'unknown')
TALK = re.compile(r'欢迎|谢谢|感谢|关注|灯牌|舰长|提督|点歌|这首歌|下一首|一首.{0,14}送给|'
                  r'大家|你们|宝宝|直播|(?:昨天|今天|明天)(?:我|我们|直播|嗓子)|刚才|我觉得|我感觉|我去|等一下|'
                  r'什么意思|是不是|对吧|好不好|哈哈|笑死')


def suggest(text, rms=None):
    """用于初始涂色的可解释启发式；所有结果仍需试听复核。"""
    compact = re.sub(r'\s+', '', text)
    if len(compact) < 12 or (rms is not None and rms < -50):
        return 'unknown', '文本太短或低能量，无法据此判定活动'
    clauses = [s for s in re.split(r'[，。！？!?；;\n]', compact) if s]
    talking = [s for s in clauses if TALK.search(s)]
    other_chars = sum(len(s) for s in clauses if not TALK.search(s))
    if talking:
        if other_chars >= 45 and len(talking) < len(clauses)/2:
            return 'mixed', '同块有互动用语和较长非互动文本，可能夹杂唱歌/讲话'
        return 'speaking', '文本出现互动或日常对话用语'
    if len(compact) >= 28:
        return 'singing', '较长连续非互动文本，疑似歌词；也可能是独白或播放录音'
    return 'unknown', '缺少足够的互动或歌词线索'


def configure_parser(commands):
    parser = commands.add_parser('activity', help='生成可试听、可修订的唱歌/讲话候选时间轴')
    actions = parser.add_subparsers(dest='action', required=True)
    build = actions.add_parser('build')
    build.add_argument('--trends', type=Path, required=True, help='trends build生成的data.json')
    build.add_argument('--asr', type=Path, required=True, help='含绝对时间和源片偏移的离线ASR JSONL')
    build.add_argument('--labels', type=Path, help='页面导出的标注修订JSON')
    build.add_argument('--audio', action='store_true', help='只读源片，按源文件生成本地试听MP3')
    build.add_argument('--output', type=Path, required=True, help='新的私有输出目录')
    verify = actions.add_parser('verify')
    verify.add_argument('--output', type=Path, required=True)


def load_windows(path, trends):
    windows, seen, origins = [], set(), {}
    lo, hi = instant(trends['since']), instant(trends['until'])
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if str(row['room_id']) != trends['room_id']:
            raise ValueError('ASR房间与趋势房间不一致')
        key = row['source_key']
        if not isinstance(key, str) or not key or key in seen:
            raise ValueError('ASR source_key为空或重复')
        seen.add(key)
        start, end = instant(row['start_utc']), instant(row['end_utc'])
        rel_start, rel_end = row['relative_start'], row['relative_end']
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (rel_start, rel_end)):
            raise ValueError('音频偏移无效')
        if end <= start or rel_start < 0 or rel_end <= rel_start or abs(end-start-rel_end+rel_start) > .02:
            raise ValueError('ASR时间与源片偏移不一致')
        identity = (row.get('sha256'), row.get('source_path'))
        origin = start-rel_start
        if identity in origins and abs(origins[identity]-origin) > .02:
            raise ValueError('同一源片的墙钟起点不一致')
        origins[identity] = origin
        if end <= lo or start >= hi:
            continue
        if not isinstance(row['text'], str):
            raise ValueError('ASR文本必须为字符串')
        label, reason = suggest(row['text'], row.get('rms_dbfs'))
        windows.append({**row, 'start': start, 'end': end, 'label': label, 'reason': reason,
                        'provenance': 'automatic_candidate'})
    return sorted(windows, key=lambda w: (w['start'], w['source_key']))


def dataset_id(windows):
    identity = [{k: w[k] for k in ('room_id', 'source_key', 'sha256', 'start_utc', 'end_utc',
                                   'relative_start', 'relative_end')} for w in windows]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def apply_labels(windows, overrides):
    known = {w['source_key']: w for w in windows}
    seen = set()
    if overrides.get('schema_version') != 1:
        raise ValueError('标注版本不支持')
    if overrides.get('dataset_id') != dataset_id(windows):
        raise ValueError('标注不属于当前房间/场次/源片数据集')
    for row in overrides['labels']:
        key = row['source_key']
        if key not in known or key in seen or row['label'] not in LABELS:
            raise ValueError('标注有重复/未知窗口或非法分类')
        if row.get('provenance') not in ('manual', 'automatic_candidate', 'semantic_review'):
            raise ValueError('标注来源无效')
        seen.add(key)
        if row['provenance'] != 'automatic_candidate':
            known[key].update(label=row['label'], provenance=row['provenance'],
                              reason=row.get('reason') or ('人工复核修订' if row['provenance'] == 'manual'
                                                          else '语义复核候选，仍需试听'))


def bands(windows, lo, hi):
    events = {}
    for i, w in enumerate(windows):
        start, end = max(lo, w['start']), min(hi, w['end'])
        if start < end:
            events.setdefault(start, []).append((i, True))
            events.setdefault(end, []).append((i, False))
    edges = sorted({lo, hi, *events})
    active, result = set(), []
    for a, b in zip(edges, edges[1:]):
        for i, add in events.get(a, []):
            active.add(i) if add else active.discard(i)
        kinds = {windows[i]['label'] for i in active}
        label = next(iter(kinds)) if len(kinds) == 1 else 'unknown'
        reason = '无ASR覆盖' if not active else '重叠窗口分类不同' if len(kinds) > 1 else ''
        if result and result[-1]['label'] == label and result[-1]['reason'] == reason:
            result[-1]['end'] = b
        else:
            result.append({'start': a, 'end': b, 'label': label, 'reason': reason})
    return result


def audio_duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)],
                            capture_output=True, text=True, check=True, timeout=30)
    duration = float(json.loads(result.stdout)['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('试听音频时长无效')
    return duration


def encode_audio(windows, output):
    sources = {}
    for w in windows:
        sha = w['sha256']
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('源片SHA256格式无效')
        source = Path(w['source_path']).resolve(strict=True)
        if sha in sources and sources[sha] != source:
            raise ValueError('同一源片哈希映射到不同路径')
        sources[sha] = source
    audio = output/'audio'
    audio.mkdir()

    def encode(pair):
        sha, path = pair
        with path.open('rb') as stream:
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != sha:
            raise ValueError(f'源片已变化：{path.name}')
        before = path.stat()
        process = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:a:0', '-vn',
                        '-af', 'aresample=async=1:first_pts=0', '-ac', '1', '-ar', '16000',
                        '-c:a', 'libmp3lame', '-b:a', '48k', str(audio/f'{sha}.mp3')],
                                 capture_output=True, text=True, timeout=180)
        if process.returncode:
            raise ValueError(f'音频导出失败 {path.name}: {process.stderr[-1000:]}')
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('编码期间源片发生变化')
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(encode, sources.items()))
    durations = {sha: audio_duration(audio/f'{sha}.mp3') for sha in sources}
    for w in windows:
        if w['relative_start'] >= durations[w['sha256']] or w['relative_end'] > durations[w['sha256']]+.2:
            raise ValueError(f'ASR偏移超出试听音频时长：{w["source_key"]}')
        w['audio'] = f'audio/{w["sha256"]}.mp3'


def verify(output):
    bundle = json.loads((output/'activity.json').read_text())
    manifest = json.loads((output/'manifest.json').read_text())
    trend = bundle['trends']
    if trend['summary'] != aggregate(trend):
        raise ValueError('趋势统计发生变化')
    current = bands(bundle['windows'], instant(trend['since']), instant(trend['until']))
    if current != bundle['bands']:
        raise ValueError('活动色带与窗口不一致')
    required = {'activity.json', 'index.html', 'labels.json'} | {w['audio'] for w in bundle['windows'] if w.get('audio')}
    if set(manifest['files']) != required:
        raise ValueError('清单缺少必需产物或引用音频')
    for name, expected in manifest['files'].items():
        path = (output/name).resolve()
        if not path.is_relative_to(output.resolve()) or digest(path) != expected:
            raise ValueError('产物指纹或路径无效')
    durations = {name: audio_duration(output/name) for name in required if name.startswith('audio/')}
    for w in bundle['windows']:
        if w.get('audio') and (w['relative_start'] >= durations[w['audio']] or
                               w['relative_end'] > durations[w['audio']]+.2):
            raise ValueError('ASR偏移超出试听音频时长')
    if (output/'index.html').read_text() != page_html(bundle):
        raise ValueError('页面与活动数据不一致')
    return {'verified': True, 'windows': len(bundle['windows']),
            'counts': dict(Counter(w['label'] for w in bundle['windows'])),
            'manual_windows': sum(w['provenance'] == 'manual' for w in bundle['windows'])}


def main(args):
    try:
        if args.action == 'verify':
            result = verify(args.output)
        else:
            if args.output.exists():
                raise ValueError('输出路径已存在，请保留旧版本并使用新目录')
            trend = json.loads(args.trends.read_text())
            if trend['summary'] != aggregate(trend):
                raise ValueError('趋势统计未通过校验')
            windows = load_windows(args.asr, trend)
            if args.labels:
                apply_labels(windows, json.loads(args.labels.read_text()))
            args.output.mkdir(parents=True)
            if args.audio:
                encode_audio(windows, args.output)
            bundle = {'schema_version': 1, 'dataset_id': dataset_id(windows), 'trends': trend, 'windows': windows,
                      'bands': bands(windows, instant(trend['since']), instant(trend['until']))}
            (args.output/'activity.json').write_text(json.dumps(bundle, ensure_ascii=False, indent=2))
            (args.output/'index.html').write_text(page_html(bundle))
            labels = {'schema_version': 1, 'dataset_id': dataset_id(windows),
                      'labels': [{k: w[k] for k in ('source_key', 'label', 'provenance', 'reason')}
                                                    for w in windows]}
            (args.output/'labels.json').write_text(json.dumps(labels, ensure_ascii=False, indent=2))
            files = {str(p.relative_to(args.output)): digest(p) for p in args.output.rglob('*') if p.is_file()}
            (args.output/'manifest.json').write_text(json.dumps({'files': files}, indent=2))
            result = {**verify(args.output), 'output': str(args.output.resolve())}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': f'{type(error).__name__}: {error}'}, ensure_ascii=False), file=sys.stderr)
        return 2
