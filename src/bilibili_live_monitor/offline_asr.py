"""离线媒体扫描、可恢复转写与语音判定。不启用 VAD，不删除输入。"""
import argparse
import array
import asyncio
import contextlib
import fcntl
import hashlib
import json
import math
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from .asr import ASR

MEDIA_SUFFIXES = {'.mka', '.mkv', '.wav', '.flac', '.mp3', '.ogg'}
DEFAULT_MODEL = 'Qwen/Qwen3-ASR-1.7B'
QUIET_RMS_DBFS = -45.0
SAMPLE_RATE = 16000


def configure_parser(subparsers):
    parser = subparsers.add_parser('offline-asr', help='离线扫描已有录制并做可恢复本地转写')
    parser.add_argument('action', choices=('scan', 'plan', 'run', 'status'))
    parser.add_argument('--input', help='录制根目录，只读')
    parser.add_argument('--output', help='状态、转写与临时解码目录')
    parser.add_argument('--dry-run', action='store_true', help='只列出将处理的文件，不启动模型')
    parser.add_argument('--asr-python', help='本地 ASR 解释器；run 非 dry-run 时必填，或由 ASR_PYTHON 提供')
    parser.add_argument('--model', default=DEFAULT_MODEL, help='本地模型 id，拒绝 URL')
    parser.add_argument('--worker-script', help='JSONL worker，默认包内 asr_worker.py')
    parser.add_argument('--max-files', type=int, help='本次最多新处理多少个已封口文件')
    parser.add_argument('--sample', type=int, help='按房间时长均匀抽取的房间数，每房间一个文件')
    parser.add_argument('--full', action='store_true', help='显式处理全部待转写文件；缺省不会跑完整库')
    parser.add_argument('--chunk-seconds', type=float, default=30)
    parser.add_argument('--probe-jobs', type=int, default=4)
    parser.add_argument('--start-timeout', type=float, default=180)
    parser.add_argument('--infer-timeout', type=float, default=180)
    return parser


def main(args):
    if args.action == 'status' or not args.output or not args.input:
        return _main(args)
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    meta = out/'inventory_meta.json'
    if meta.exists() and json.loads(meta.read_text())['input_root'] != str(Path(args.input).resolve()):
        return _fail('输出目录绑定了不同input；请使用独立output')
    lock = _acquire_lock(out)
    if lock is None:
        return _fail('已有 offline-asr 操作持有该输出目录')
    previous_handler = signal.getsignal(signal.SIGTERM)

    def terminate(*_):
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    try:
        return _main(args)
    except (ValueError, FileNotFoundError) as error:
        return _fail(str(error))
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
        _release_lock(lock)


def _main(args):
    if args.action == 'status':
        if not args.output:
            return _fail('status 需要 --output')
        return cmd_status(Path(args.output))
    if not args.input or not args.output:
        return _fail('scan/plan/run 需要 --input 与 --output')
    if _bad_model(args.model):
        return _fail('拒绝非本地模型 id')
    if (not math.isfinite(args.chunk_seconds) or args.chunk_seconds <= 0 or args.probe_jobs < 1
            or args.sample is not None and args.sample < 1):
        return _fail('chunk-seconds 与 probe-jobs 必须为正')
    root = Path(args.input).resolve()
    out = Path(args.output).resolve()
    if not root.is_dir():
        return _fail('input 不是目录')
    out.mkdir(parents=True, exist_ok=True)
    if args.action == 'scan':
        rows = scan(root, args.probe_jobs)
        _atomic_json(out/'inventory_meta.json', {'input_root': str(root)})
        _write_jsonl(out / 'inventory.jsonl', rows)
        summary = _summary(rows)
        (out / 'inventory_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    rows = _load_inventory(out, root, args.probe_jobs)
    if args.action == 'plan':
        plan = build_plan(rows, args.sample)
        (out / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        _sync(out, rows, plan)
        print(json.dumps({'sample_files': len(plan['sample_paths']), 'sealed': plan['sealed_files']}, ensure_ascii=False))
        return 0
    if args.action != 'run':
        return _fail('未知动作')
    plan = build_plan(rows, args.sample)
    (out / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    _sync(out, rows, plan)
    selected = pending_rows(out, rows, args)
    if isinstance(selected, int):
        return selected
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'files': [row['path'] for row in selected]}, ensure_ascii=False, indent=2))
        return 0
    if not selected:
        _write_status(out, _db(out))
        return 0
    python = args.asr_python or os.getenv('ASR_PYTHON')
    if not python:
        return _fail('run 需要 --asr-python 或 ASR_PYTHON')
    worker = Path(args.worker_script) if args.worker_script else Path(__file__).with_name('asr_worker.py')
    if not worker.is_file() or not Path(python).is_file():
        return _fail('asr-python 或 worker-script 不存在')
    return run_files(out, selected, python, worker, args)


def scan(root, jobs):
    files = sorted(path for path in root.rglob('*') if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES)
    if jobs == 1 or len(files) < 2:
        return [probe(root, path) for path in files]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(lambda path: probe(root, path), files))


def probe(root, path):
    stat = path.stat()
    relative = path.relative_to(root)
    item = {
        'path': str(path),
        'relative': str(relative),
        'run': relative.parts[0] if len(relative.parts) > 2 else '',
        'room_id': path.parent.name,
        'name': path.name,
        'suffix': path.suffix.lower(),
        'size': stat.st_size,
        'mtime_ns': stat.st_mtime_ns,
    }
    item['sha256'] = _sha256(path)
    try:
        proc = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except subprocess.TimeoutExpired:
        item.update(sealed='probe_timeout', probe_ok=False, duration_sec=None)
        return item
    if proc.returncode != 0:
        item.update(sealed='probe_failed', probe_ok=False, duration_sec=None, error=(proc.stderr or '')[-300:])
        return item
    payload = json.loads(proc.stdout or '{}')
    audio = [stream for stream in payload.get('streams') or [] if stream.get('codec_type') == 'audio']
    duration = (payload.get('format') or {}).get('duration')
    try:
        duration_sec = float(duration) if duration is not None else None
    except ValueError:
        duration_sec = None
    item.update(
        probe_ok=True,
        sealed='sealed_readable' if audio and duration_sec is not None and duration_sec > 0 else 'unsealed_or_incomplete',
        duration_sec=duration_sec,
        audio_codec=audio[0].get('codec_name') if audio else None,
    )
    return item


def build_plan(rows, sample):
    sealed = [row for row in rows if row.get('sealed') == 'sealed_readable' and row.get('duration_sec')]
    by_room = {}
    for row in sealed:
        by_room.setdefault(row['room_id'], []).append(row)
    rooms = sorted(by_room, key=lambda room: (sum(item['duration_sec'] for item in by_room[room]), room))
    count = min(sample or 0, len(rooms))
    picked = []
    if count:
        indexes = [0] if count == 1 else [round(i * (len(rooms) - 1) / (count - 1)) for i in range(count)]
        seen = set()
        for index in indexes:
            room = rooms[index]
            if room not in seen:
                picked.append(room)
                seen.add(room)
        for room in rooms:
            if len(picked) >= count:
                break
            if room not in seen:
                picked.append(room)
                seen.add(room)
    fracs = (0.0, 0.5, 1.0)
    sample_paths = []
    for index, room in enumerate(picked):
        ordered = sorted(by_room[room], key=lambda row: row['name'])
        usable = [row for row in ordered if row['duration_sec'] >= 1] or ordered
        chosen = usable[min(len(usable) - 1, round(fracs[index % 3] * (len(usable) - 1)))]
        sample_paths.append(chosen['path'])
    return {
        'generated_utc': _now(),
        'sealed_files': len(sealed),
        'room_count': len(by_room),
        'sample_rooms': picked,
        'sample_paths': sample_paths,
        'vad': False,
    }


def run_files(out, selected, python, worker, args):
    if not selected:
        _write_status(out, _db(out))
        return 0
    session = None
    failures = 0
    try:
        session = Worker(python, worker, args.model, out / 'logs' / 'asr_worker.stderr', args.start_timeout, args.infer_timeout)
        session.start()
        db = _db(out)
        for row in selected:
            db.execute(
                "UPDATE jobs SET status='running', started_utc=? WHERE path=?",
                (_now(), row['path']),
            )
            db.commit()
            try:
                payload = transcribe_file(session, out, row, args.chunk_seconds)
                dest = _transcript_path(out, row)
                _atomic_json(dest, payload)
                db.execute(
                    """UPDATE jobs SET status='done', error=NULL, audio_sec=?, infer_sec=?, transcript_path=?, finished_utc=?
                       WHERE path=?""",
                    (payload['decoded_sec'], payload['infer_sec'], str(dest), _now(), row['path']),
                )
            except Exception as exc:
                failures += 1
                db.execute(
                    "UPDATE jobs SET status='failed', error=?, finished_utc=? WHERE path=?",
                    (f'{type(exc).__name__}: {exc}', _now(), row['path']),
                )
            db.commit()
            _write_status(out, db)
        _write_status(out, db)
        return 1 if failures else 0
    finally:
        if session:
            session.close()


def transcribe_file(session, out, row, chunk_seconds):
    _verify_identity(row)
    before = Path(row['path']).stat()
    pcm = decode_pcm(row['path'])
    after = Path(row['path']).stat()
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise RuntimeError('输入在解码期间发生变化')
    work = out / 'work' / row['sha256']
    work.mkdir(parents=True, exist_ok=True)
    chunks = []
    infer = 0.0
    width = chunk_seconds * SAMPLE_RATE * 4
    total = max(1, math.ceil(len(pcm) / width)) if pcm else 1
    pieces = [pcm[index * int(width):(index + 1) * int(width)] for index in range(total)] if pcm else [b'']
    try:
        for index, piece in enumerate(pieces):
            start = index * chunk_seconds
            end = start + (len(piece) / 4 / SAMPLE_RATE if piece else 0)
            rms = rms_dbfs(piece)
            wav = work / f'{index:05d}.wav'
            _write_wav(wav, piece)
            try:
                result = session.request(wav, f'{row["sha256"]}:{index}')
            finally:
                wav.unlink(missing_ok=True)
            text = result.get('text') or ''
            infer += (result.get('infer_ms') or 0) / 1000
            chunks.append({
                'start': round(start, 3),
                'end': round(end, 3),
                'text': text,
                'rms_dbfs': rms,
                'text_evidence': text_evidence(text, rms),
                'truncated': bool(result.get('truncated')),
                'error': result.get('error'),
            })
    finally:
        with contextlib.suppress(OSError):
            work.rmdir()
    counts = {}
    for chunk in chunks:
        counts[chunk['text_evidence']] = counts.get(chunk['text_evidence'], 0) + 1
    return {
        'path': row['path'],
        'room_id': row['room_id'],
        'sha256': row['sha256'],
        'size': row['size'],
        'mtime_ns': row['mtime_ns'],
        'probe_duration_sec': row.get('duration_sec'),
        'decoded_sec': round(len(pcm) / 4 / SAMPLE_RATE, 3),
        'decode': 'ffmpeg_aresample_async_1_timeline',
        'model': session.model,
        'vad': False,
        'infer_sec': round(infer, 3),
        'has_text': any(chunk['text'].strip() for chunk in chunks),
        'speech_presence': 'unknown',
        'text_evidence_counts': counts,
        'chunks': chunks,
        'finished_utc': _now(),
    }


def text_evidence(text, rms):
    if not (text or '').strip():
        if rms is not None and rms >= QUIET_RMS_DBFS:
            return 'energy_without_text'
        return 'no_text'
    if rms is None or rms < QUIET_RMS_DBFS:
        return 'low_energy_text'
    return 'text_with_energy'


def decode_command(path):
    return ['ffmpeg', '-v', 'error', '-i', str(path), '-af', 'aresample=async=1:first_pts=0',
            '-ac', '1', '-ar', str(SAMPLE_RATE), '-f', 'f32le', '-']


def decode_pcm(path):
    proc = subprocess.run(decode_command(path), capture_output=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or b'').decode(errors='replace')[-400:])
    if len(proc.stdout) % 4:
        raise RuntimeError('decoded_pcm_not_aligned')
    return proc.stdout


def rms_dbfs(pcm):
    count = len(pcm) // 4
    if count == 0:
        return None
    samples = array.array('f')
    samples.frombytes(pcm[:count * 4])
    total = 0.0
    for sample in samples:
        total += sample * sample
    return round(20 * math.log10(math.sqrt(total / count) + 1e-12), 2)


class Worker:
    def __init__(self, python, script, model, log_path, start_timeout, infer_timeout):
        self.python = python
        self.script = script
        self.model = model
        self.log_path = log_path
        self.start_timeout = start_timeout
        self.infer_timeout = infer_timeout
        self.runner = asyncio.Runner()
        self.worker = ASR(SimpleNamespace(data_dir=log_path.parent, asr_python=python, asr_model=model,
                                          asr_start_timeout=start_timeout, asr_infer_timeout=infer_timeout), script)

    def start(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        os.environ.update({
            'ASR_MODEL': self.model,
            'HF_HUB_OFFLINE': '1',
            'HF_HUB_DISABLE_TELEMETRY': '1',
            'TRANSFORMERS_OFFLINE': '1',
        })
        self.runner.run(self.worker.start())

    def request(self, wav, req_id):
        result = self.runner.run(self.worker.transcribe(wav, req_id))
        if result.get('error'):
            raise RuntimeError(result['error'])
        return result

    def close(self):
        try:
            self.runner.run(self.worker.close())
        finally:
            self.runner.close()


def cmd_status(out):
    db_path = out / 'state' / 'jobs.sqlite3'
    if not db_path.exists():
        print(json.dumps({'ready': False}, ensure_ascii=False))
        return 0
    db = sqlite3.connect(db_path)
    counts = dict(db.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status'))
    audio, infer = db.execute(
        "SELECT COALESCE(SUM(audio_sec),0), COALESCE(SUM(infer_sec),0) FROM jobs WHERE status='done'"
    ).fetchone()
    print(json.dumps({
        'ready': True,
        'counts': counts,
        'audio_sec_done': round(audio, 3),
        'infer_sec_done': round(infer, 3),
        'rtf': round(infer / audio, 5) if audio else None,
        'vad': False,
    }, ensure_ascii=False, indent=2))
    return 0


def pending_rows(out, rows, args):
    if not args.full and not args.max_files and not args.sample:
        _fail('run 需要 --full、--max-files 或 --sample，避免默认跑完整库')
        return 2
    if args.max_files is not None and args.max_files < 1:
        _fail('--max-files 至少为 1')
        return 2
    plan = build_plan(rows, args.sample)
    sample = set(plan['sample_paths'])
    sealed = [row for row in rows if row.get('sealed') == 'sealed_readable' and row.get('duration_sec')]
    chosen = [row for row in sealed if row['path'] in sample] if args.sample else sealed
    db = _db(out)
    pending = [row for row in chosen if not _done_same(db, row)]
    if args.max_files is not None:
        pending = pending[:args.max_files]
    return pending


def _sync(out, rows, plan):
    sample = set(plan['sample_paths'])
    db = _db(out)
    for row in rows:
        priority = 0 if row['path'] in sample else 1 if row.get('sealed') == 'sealed_readable' else 9
        existing = db.execute('SELECT size, mtime_ns, sha256, status FROM jobs WHERE path=?', (row['path'],)).fetchone()
        if existing and existing[:3] == (row['size'], row['mtime_ns'], row['sha256']) and existing[3] in {'done', 'skipped'}:
            continue
        status = 'skipped' if priority == 9 else 'pending'
        db.execute(
            """INSERT INTO jobs (path, run, room_id, name, size, mtime_ns, sha256, duration_sec, sealed, priority, status, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 size=excluded.size, mtime_ns=excluded.mtime_ns, sha256=excluded.sha256, duration_sec=excluded.duration_sec,
                 sealed=excluded.sealed, priority=excluded.priority, status=excluded.status, error=excluded.error,
                 audio_sec=NULL, infer_sec=NULL, transcript_path=NULL, started_utc=NULL, finished_utc=NULL""",
            (row['path'], row.get('run'), row['room_id'], row['name'], row['size'], row['mtime_ns'], row['sha256'],
             row.get('duration_sec'), row.get('sealed'), priority, status, None if priority < 9 else row.get('sealed')),
        )
    db.commit()


def _db(out):
    path = out / 'state' / 'jobs.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            path TEXT PRIMARY KEY, run TEXT, room_id TEXT, name TEXT, size INTEGER, mtime_ns INTEGER,
            sha256 TEXT, duration_sec REAL, sealed TEXT, priority INTEGER, status TEXT, error TEXT,
            audio_sec REAL, infer_sec REAL, transcript_path TEXT, started_utc TEXT, finished_utc TEXT
        )"""
    )
    return db


def _done_same(db, row):
    found = db.execute('SELECT size, mtime_ns, sha256, status FROM jobs WHERE path=?', (row['path'],)).fetchone()
    return bool(found and found[:3] == (row['size'], row['mtime_ns'], row['sha256']) and found[3] == 'done')


def _load_inventory(out, root, jobs):
    path = out / 'inventory.jsonl'
    meta = out/'inventory_meta.json'
    if meta.exists() and json.loads(meta.read_text())['input_root'] != str(root):
        raise ValueError('输出目录绑定了不同input；请使用独立output')
    if not path.exists():
        rows = scan(root, jobs)
        _write_jsonl(path, rows)
        _atomic_json(meta, {'input_root': str(root)})
        return rows
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for row in rows:
        if not Path(row['path']).resolve().is_relative_to(root):
            raise ValueError('inventory不属于本次input')
        _verify_identity(row)
    return rows


def _verify_identity(row):
    path = Path(row['path'])
    stat = path.stat()
    if (stat.st_size, stat.st_mtime_ns, _sha256(path)) != (row['size'], row['mtime_ns'], row['sha256']):
        raise ValueError('素材已变化，请显式scan后恢复：'+str(path))


def _summary(rows):
    durations = [row['duration_sec'] for row in rows if row.get('duration_sec') is not None]
    sealed = {}
    for row in rows:
        sealed[row.get('sealed')] = sealed.get(row.get('sealed'), 0) + 1
    return {
        'files': len(rows),
        'bytes': sum(row['size'] for row in rows),
        'duration_sec': round(sum(durations), 3),
        'rooms': len({row['room_id'] for row in rows}),
        'sealed': sealed,
        'vad': False,
    }


def _write_status(out, db):
    counts = dict(db.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status'))
    audio, infer = db.execute(
        "SELECT COALESCE(SUM(audio_sec),0), COALESCE(SUM(infer_sec),0) FROM jobs WHERE status='done'"
    ).fetchone()
    _atomic_json(out / 'status.json', {
        'updated_utc': _now(),
        'counts': counts,
        'audio_sec_done': round(audio, 3),
        'infer_sec_done': round(infer, 3),
        'rtf': round(infer / audio, 5) if audio else None,
        'vad': False,
    })


def _acquire_lock(out):
    path = out / 'state' / 'run.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _release_lock(handle):
    if handle:
        handle.close()  # 不unlink持锁文件，避免等待者持有旧inode后出现双持有者。


def _transcript_path(out, row):
    digest = hashlib.sha256((row['path']+':'+row['sha256']).encode()).hexdigest()
    return out / 'transcripts' / digest[:2] / f'{digest}.json'


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _write_jsonl(path, rows):
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))


def _write_wav(path, pcm):
    import wave

    samples = array.array('f')
    if pcm:
        samples.frombytes(pcm[:len(pcm) // 4 * 4])
    frames = array.array('h', (max(-32767, min(32767, int(sample * 32767))) for sample in samples))
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(frames.tobytes())


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _bad_model(model):
    text = (model or '').strip().lower()
    return not text or '://' in text or text.startswith('http')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _fail(message):
    print(message, file=sys.stderr)
    return 2


def _cli(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    configure_parser(commands)
    args = parser.parse_args(argv)
    return main(args)


if __name__ == '__main__':
    sys.exit(_cli())
