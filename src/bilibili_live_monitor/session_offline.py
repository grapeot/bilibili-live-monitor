"""只处理本场录制文件；离线转写独立保存，绝不覆盖采集数据库。"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from . import offline_asr
from .live_share import atomic_json, epoch, iso

NAME = re.compile(r'^[a-f0-9]+-(\d{8}T\d{6}Z)-([a-f0-9]+)-(\d{5})\.(?:mkv|mka|wav)$')


def recording_files(config, cutoff):
    from .archive_media import source_paths
    groups = {}
    lower = epoch(config['start_utc'])
    for path in source_paths(Path(config['recording_root']), {'.mkv', '.mka', '.wav'}):
        match = NAME.fullmatch(path.name)
        if not match:
            continue
        start = datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).timestamp()
        if lower - 1 <= start <= cutoff:
            groups.setdefault((start, match[2]), []).append((int(match[3]), path))
    return groups


def run_offline(config, state):
    workspace = Path(config['workspace'])
    out = workspace / 'offline'
    out.mkdir(exist_ok=True)
    manifest_path = out / 'manifest.json'
    cutoff = state['cutoff']
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest['cutoff'] != cutoff:
            raise ValueError('离线素材已绑定不同截止点')
    else:
        rows = []
        for (start, group), files in sorted(recording_files(config, cutoff).items()):
            for index, path in sorted(files):
                row = offline_asr.probe(Path(config['recording_root']), path)
                row.update(room_id=config['room_id'], run_start=start, run_group=group, segment_index=index)
                rows.append(row)
        manifest = {'cutoff': cutoff, 'rows': rows, 'mapping': 'filename UTC + preceding media durations (estimated)'}
    rows = manifest['rows']
    if not rows:
        raise RuntimeError('本场没有已识别命名的录制文件')
    for row in rows:
        if row.get('recovery_original_identity'):
            offline_asr._verify_identity(row['recovery_original_identity'])
        offline_asr._verify_identity(row)
        if row.get('sealed') != 'sealed_readable':
            # 仅重探测失败项；不让瞬时ffprobe失败变成永久skipped。
            row.update(offline_asr.probe(Path(config['recording_root']), Path(row['path'])))
            row['room_id'] = config['room_id']
        if row.get('sealed') != 'sealed_readable':
            from .media_recovery import recover
            row.update(recover(row, workspace))
    cursors = {}
    for row in rows:
        key = (row['run_start'], row['run_group'])
        cursor, previous = cursors.get(key, (row['run_start'], -1))
        cursor += max(0, row['segment_index'] - previous - 1) * config['recording_segment_seconds']
        row['wall_start'] = cursor
        cursors[key] = (cursor + (row.get('duration_sec') or config['recording_segment_seconds']), row['segment_index'])
    atomic_json(manifest_path, manifest)
    unavailable = sum(row.get('sealed') != 'sealed_readable' for row in rows)
    if unavailable:
        raise RuntimeError(f'{unavailable}个本场文件尚不可读取，保留清单并重试，不将部分稿当作全场完成')
    args = SimpleNamespace(full=True, sample=None, max_files=None, model=config['asr_model'],
                           chunk_seconds=30.1, start_timeout=180, infer_timeout=180)
    offline_asr._sync(out, rows, offline_asr.build_plan(rows, None))
    pending = offline_asr.pending_rows(out, rows, args)
    if pending:
        result = offline_asr.run_files(out, pending, config['asr_python'],
                                       Path(offline_asr.__file__).with_name('asr_worker.py'), args)
        if result:
            raise RuntimeError('离线ASR存在失败文件，保留进度等待重试')
    db = offline_asr._db(out)
    try:
        transcripts = dict(db.execute("SELECT path,transcript_path FROM jobs WHERE status='done'"))
    finally:
        db.close()
    if any(row['path'] not in transcripts for row in rows):
        raise RuntimeError('离线完成清单未覆盖全部本场源文件')
    segments = []
    for row in rows:
        payload = json.loads(Path(transcripts[row['path']]).read_text())
        for index, chunk in enumerate(payload['chunks']):
            start, end = row['wall_start'] + chunk['start'], row['wall_start'] + chunk['end']
            if start < epoch(config['start_utc']) or end > cutoff:
                continue  # 不将跨边界文本贴到本场。
            key = f'{row["sha256"]}:{index}'
            segments.append({'seq': -int(hashlib.sha256(key.encode()).hexdigest()[:15], 16),
                'start_utc': iso(start), 'end_utc': iso(end), 'text': chunk['text'], 'is_final': True})
    if not segments:
        raise RuntimeError('离线ASR没有本场范围内的可用窗口')
    destination = workspace / 'offline_segments.json'
    atomic_json(destination, sorted(segments, key=lambda s: s['start_utc']))
    atomic_json(out / 'completed.json', {
        'room_id': config['room_id'], 'start_utc': config['start_utc'], 'cutoff': cutoff,
        'files': len(rows), 'segments': len(segments),
        'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        'segments_sha256': hashlib.sha256(destination.read_bytes()).hexdigest()})
    return str(destination)


def verify_offline(config, cutoff):
    workspace = Path(config['workspace'])
    manifest_path, marker_path = workspace / 'offline/manifest.json', workspace / 'offline/completed.json'
    destination = workspace / 'offline_segments.json'
    if Path(config.get('offline_segments', '')).resolve() != destination.resolve():
        raise ValueError('离线转写尚未接入本场')
    marker = json.loads(marker_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    segments = json.loads(destination.read_text())
    status = json.loads((workspace / 'offline/status.json').read_text())
    if (marker.get('room_id') != config['room_id'] or marker.get('start_utc') != config['start_utc']
            or marker.get('cutoff') != cutoff or manifest.get('cutoff') != cutoff
            or not manifest.get('rows') or marker.get('files') != len(manifest['rows'])
            or not segments or marker.get('segments') != len(segments)
            or status.get('counts', {}).get('done') != len(manifest['rows'])
            or any(value for key, value in status.get('counts', {}).items() if key != 'done')
            or marker.get('manifest_sha256') != hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            or marker.get('segments_sha256') != hashlib.sha256(destination.read_bytes()).hexdigest()):
        raise ValueError('离线转写完成凭证不匹配')
    return marker
