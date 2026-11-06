"""直播中回填已封口素材；不修改原库、最终封口清单或场次阶段。"""
import argparse
import fcntl
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from . import offline_asr
from .live_share import atomic_json, epoch, iso
from .live_share_control import load_config
from .session_offline import recording_files


def prepare(config, cutoff):
    from .archive_media import archive_receipt, resolve_media
    rows, skipped = [], []
    root = Path(config['recording_root'])
    for (start, group), files in sorted(recording_files(config, cutoff).items()):
        cursor, previous = start, -1
        for index, path in sorted(files):
            cursor += max(0, index - previous - 1) * config['recording_segment_seconds']
            physical = resolve_media(path)
            before = physical.stat()
            source_mtime = (before.st_mtime if physical == path
                            else archive_receipt(path)['source_stat']['mtime_ns'] / 1e9)
            # 正在写入的尾片不做哈希或探测，更不送入离线模型。
            if source_mtime > cutoff - 10:
                row = {'path': str(path), 'duration_sec': None, 'sealed': 'recent_write'}
            else:
                row = offline_asr.probe(root, path)
                after = physical.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    row['sealed'] = 'changed_during_probe'
            row.update(room_id=config['room_id'], run_start=start, run_group=group,
                       segment_index=index, wall_start=cursor)
            end = cursor + (row.get('duration_sec') or config['recording_segment_seconds'])
            if row['sealed'] == 'sealed_readable' and end <= cutoff:
                rows.append(row)
            else:
                skipped.append({'path': str(path), 'reason': row['sealed'], 'wall_start': cursor})
            cursor, previous = end, index
    return {'cutoff': cutoff, 'rows': rows, 'skipped': skipped,
            'mapping': 'filename UTC + preceding media durations (estimated)'}


def export(config, out, manifest):
    db = offline_asr._db(out)
    try:
        transcripts = dict(db.execute("SELECT path,transcript_path FROM jobs WHERE status='done'"))
    finally:
        db.close()
    segments = []
    for row in manifest['rows']:
        if row['path'] not in transcripts:
            continue
        offline_asr._verify_identity(row)
        payload = json.loads(Path(transcripts[row['path']]).read_text())
        for index, chunk in enumerate(payload['chunks']):
            start, end = row['wall_start'] + chunk['start'], row['wall_start'] + chunk['end']
            if start < epoch(config['start_utc']) or end > manifest['cutoff']:
                continue
            key = f'{row["sha256"]}:{index}'
            segments.append({'seq': -int(hashlib.sha256(key.encode()).hexdigest()[:15], 16),
                             'start_utc': iso(start), 'end_utc': iso(end),
                             'text': chunk['text'], 'is_final': True})
    if segments:
        atomic_json(Path(config['workspace']) / 'live_backfill_segments.json',
                    sorted(segments, key=lambda row: row['start_utc']))
    return len(segments)


def run(config, *, cutoff=None, dry_run=False):
    out = Path(config['workspace']) / 'live_backfill'
    out.mkdir(exist_ok=True)
    with (out / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = out / 'manifest.json'
        manifest = json.loads(path.read_text()) if path.exists() else None
        if not manifest or not manifest.get('rows'):
            manifest = prepare(config, cutoff or time.time())
        summary = {'cutoff': iso(manifest['cutoff']), 'files': len(manifest['rows']),
                   'audio_seconds': sum(r['duration_sec'] for r in manifest['rows']),
                   'skipped_files': len(manifest['skipped']), 'dry_run': dry_run}
        if dry_run:
            return summary
        rows = manifest['rows']
        if not rows:
            raise RuntimeError('本场暂时没有已封口素材')
        atomic_json(path, manifest)
        for row in rows:
            offline_asr._verify_identity(row)
        args = SimpleNamespace(full=True, sample=None, max_files=None, model=config['asr_model'],
                               chunk_seconds=30.1, start_timeout=180, infer_timeout=180)
        offline_asr._sync(out, rows, offline_asr.build_plan(rows, None))
        pending = offline_asr.pending_rows(out, rows, args)
        result = offline_asr.run_files(out, pending, config['asr_python'],
                                      Path(offline_asr.__file__).with_name('asr_worker.py'), args) if pending else 0
        summary.update(segments=export(config, out, manifest), status='failed' if result else 'completed',
                       updated_at=iso(time.time()))
        atomic_json(out / 'backfill_status.json', summary)
        if result:
            raise RuntimeError('部分离线素材失败；已保存成功窗口和断点，重跑同一命令可继续')
        return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(load_config(args.config), dry_run=args.dry_run), ensure_ascii=False))


if __name__ == '__main__':
    main()
