"""独立周期录像维护；与手动归档共锁，最多五个并行作业，失败保留原片。"""
import argparse
import json
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from . import recording_archive as archive
from .archive_media import archived_input, file_stat
from .live_share import atomic_json


class ArchiveWriterBusy(BlockingIOError):
    """已有归档持锁；周期任务可明确skip，而非吞掉其他IO错误。"""


def plan(roots):
    items = {}
    references = {}
    for root in roots:
        for directory in sorted(root.iterdir()):
            workflow = directory / 'workflow.json'
            manifest = directory / 'offline/manifest.json'
            if workflow.is_file() and manifest.is_file():
                stage = json.loads(workflow.read_text()).get('stage')
                for row in json.loads(manifest.read_text()).get('rows', []):
                    source = str(Path(row['path']).resolve())
                    references.setdefault(source, []).append((str(workflow), stage, row))
        for item in archive.inventory(root):
            source = item['source']
            if source not in items:
                items[source] = item
    for source, item in items.items():
        refs = references[source]
        item['workflows'] = sorted({ref[0] for ref in refs})
        if any(stage != 'completed' for _, stage, _ in refs):
            item.update(action='skip', reason='referenced_by_unfinished_session')
        elif any({key: row[key] for key in ('size', 'mtime_ns')} != item['source_stat']
                 or row.get('sha256') != item['legacy_row'].get('sha256') for _, _, row in refs):
            item.update(action='skip', reason='conflicting_source_identity')
    return list(items.values())


def process(item, locks, *, trash_originals):
    started = time.monotonic()
    try:
        try:
            result = (archive.encode(item, locks) if item['action'] == 'encode'
                      else {'source': item['source'], 'status': 'already_archived'})
        except archive.ArchiveSkip as skip:
            result = archive.retain_original(item, skip)
            result['seconds'] = round(time.monotonic() - started, 3)
            return result
        # 核对旧manifest身份以及实际归档读取入口，之后才允许Trash。
        target = archived_input(item['legacy_row'])
        if target.stat().st_size >= item['source_stat']['size']:
            raise ValueError('归档没有变小，保留原片')
        if trash_originals:
            result.update(archive.cleanup(item, permanent=False))
            if Path(item['source']).exists():
                raise ValueError('Trash返回成功但原片仍存在')
            archived_input(item['legacy_row'])
        result['archive_bytes'] = file_stat(target)['size']
        result['source_bytes'] = item['source_stat']['size']
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        result = {'source': item['source'], 'status': 'failed', 'error': str(error),
                  'original_present': Path(item['source']).exists()}
        stderr = getattr(error, 'stderr', None)
        if stderr:
            result['stderr'] = stderr.decode(errors='replace') if isinstance(stderr, bytes) else stderr
    result['seconds'] = round(time.monotonic() - started, 3)
    return result


def run(roots, *, execute=False, workers=2, trash_originals=False, report_dir=None, max_files=None):
    if workers not in range(1, 6) or max_files is not None and max_files < 1:
        raise ValueError('workers只支持1至5；max-files须为正数')
    roots = sorted({Path(root).resolve(strict=True) for root in roots})
    if not roots:
        raise ValueError('至少指定一个sessions-root')
    if execute and report_dir is None:
        raise ValueError('执行时必须指定report-dir')
    with ExitStack() as stack:
        # 先拿全部root锁再盘点；排序避免死锁，FFmpeg继承全部锁防止孤儿编码器重叠。
        try:
            locks = tuple(stack.enter_context(archive.writer_lock(root)) for root in roots) if execute else ()
        except BlockingIOError as error:
            raise ArchiveWriterBusy('已有归档作业持锁') from error
        items = plan(roots)
        pending = [item for item in items if item['action'] == 'encode'
                   or trash_originals and item['action'] == 'archived' and Path(item['source']).is_file()]
        pending = pending[:max_files] if max_files is not None else pending
        summary = {'read_only': not execute, 'workers': workers, 'trash_originals': trash_originals,
                    'inventory': dict(Counter(item['action'] for item in items)),
                   'skip_reasons': dict(Counter(item['reason'] for item in items if item['action'] == 'skip')),
                   'retained_warnings': sum(bool(item.get('warning')) for item in items if item['action'] == 'skip'),
                   'planned': len(pending), 'source_bytes': sum(item['source_stat']['size'] for item in pending),
                   'duration_seconds': sum(item.get('duration') or 0 for item in pending)}
        if not execute:
            return {**summary, 'items': items, 'results': []}
        report_dir = Path(report_dir)
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        run_dir = report_dir / stamp
        run_dir.mkdir()
        atomic_json(run_dir / 'plan.json', {**summary, 'items': items})
        results = []

        def checkpoint(complete=False):
            state = {**summary, 'read_only': False, 'run_dir': str(run_dir),
                     'updated_utc': datetime.now(timezone.utc).isoformat(), 'complete': complete,
                     'done': len(results), 'counts': dict(Counter(r['status'] for r in results)),
                     'results': results}
            atomic_json(run_dir / 'progress.json', state)
            atomic_json(report_dir / 'latest.json', state)
            return state

        checkpoint()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(process, item, locks, trash_originals=trash_originals) for item in pending]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                checkpoint()
                print(json.dumps(result, ensure_ascii=False), flush=True)
        return checkpoint(complete=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions-root', type=Path, action='append', required=True)
    parser.add_argument('--workers', type=int, choices=range(1, 6), default=2)
    parser.add_argument('--trash-originals', action='store_true')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--report-dir', type=Path)
    parser.add_argument('--max-files', type=int)
    args = parser.parse_args()
    try:
        result = run(args.sessions_root, execute=args.execute, workers=args.workers,
                     trash_originals=args.trash_originals, report_dir=args.report_dir, max_files=args.max_files)
    except ArchiveWriterBusy:
        print(json.dumps({'skipped': True, 'reason': 'archive_writer_busy'}))
        return 0
    print(json.dumps(result, ensure_ascii=False))
    return 1 if any(row['status'] == 'failed' for row in result['results']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
