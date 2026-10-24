import argparse
import json
import math
import os
import sqlite3
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest

from bilibili_live_monitor.offline_asr import (
    QUIET_RMS_DBFS,
    _acquire_lock,
    _release_lock,
    configure_parser,
    decode_command,
    main,
    text_evidence,
)


def test_configure_parser_exposes_offline_asr():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    configure_parser(commands)
    args = parser.parse_args(['offline-asr', 'status', '--output', 'out'])
    assert args.command == 'offline-asr'
    assert args.action == 'status'
    assert args.full is False


def test_run_refuses_unbounded_library(tmp_path):
    _tone(tmp_path / 'recordings' / '123456' / 'a.wav')
    code = main(_args('run', tmp_path, tmp_path / 'out'))
    assert code == 2
    assert not (tmp_path / 'out' / 'logs' / 'asr_worker.stderr').exists()


def test_dry_run_does_not_start_worker(tmp_path):
    _tone(tmp_path / 'recordings' / '123456' / 'a.wav')
    code = main(_args('run', tmp_path, tmp_path / 'out', dry_run=True, max_files=1, asr_python='missing-python'))
    assert code == 0
    payload = json.loads((tmp_path / 'out' / 'plan.json').read_text())
    assert payload['vad'] is False
    assert not (tmp_path / 'out' / 'logs').exists()


def test_decode_command_fills_timeline_gaps():
    command = decode_command('clip.mka')
    assert 'aresample=async=1:first_pts=0' in command
    assert command[0] == 'ffmpeg'


def test_energy_and_text_are_observations_not_speech_truth():
    assert text_evidence('hello', QUIET_RMS_DBFS - 1) == 'low_energy_text'
    assert text_evidence('', -10) == 'energy_without_text'
    assert text_evidence('hello', -20) == 'text_with_energy'


def test_run_resume_speech_claim_and_keeps_input(tmp_path):
    source = tmp_path / 'recordings' / '123456'
    tone = source / 'tone.wav'
    quiet = source / 'quiet.wav'
    _tone(tone)
    _silence(quiet)
    before = {path.name: path.read_bytes() for path in (tone, quiet)}
    worker = tmp_path / 'fake_worker.py'
    worker.write_text(
        'import json,sys\n'
        'print(json.dumps({"ready": True}), flush=True)\n'
        'for line in sys.stdin:\n'
        '    req=json.loads(line)\n'
        '    print(json.dumps({"req_id": req["req_id"], "text": "hello", "infer_ms": 3, '
        '"truncated": False, "error": None}), flush=True)\n'
    )
    out = tmp_path / 'out'
    code = main(_args(
        'run', tmp_path, out, max_files=2, asr_python=_python(), worker_script=str(worker),
        start_timeout=10, infer_timeout=10,
    ))
    assert code == 0
    transcripts = list((out / 'transcripts').rglob('*.json'))
    assert len(transcripts) == 2
    by_name = {}
    for path in transcripts:
        payload = json.loads(path.read_text())
        by_name[Path(payload['path']).name] = payload
        assert payload['vad'] is False
        assert Path(payload['path']).read_bytes() == before[Path(payload['path']).name]
    assert by_name['tone.wav']['has_text'] is True
    assert by_name['quiet.wav']['has_text'] is True
    assert all(item['speech_presence']=='unknown' for item in by_name.values())
    assert by_name['quiet.wav']['text_evidence_counts']['low_energy_text'] == 1
    again = main(_args(
        'run', tmp_path, out, max_files=2, asr_python='/bin/false', worker_script=str(worker),
    ))
    assert again == 0
    status = json.loads(main_capture(tmp_path, out))
    assert status['counts']['done'] == 2
    lock = _acquire_lock(out)
    assert lock is not None
    _release_lock(lock)


def test_inventory_identity_changes_require_explicit_rescan(tmp_path):
    source = tmp_path/'recordings'
    audio = source/'123456'/'a.wav'
    _tone(audio)
    out = tmp_path/'out'
    assert main(_args('plan',source,out))==0
    _silence(audio)
    assert main(_args('run',source,out,dry_run=True,max_files=1))==2
    assert main(_args('scan',source,out))==0
    assert main(_args('run',source,out,dry_run=True,max_files=1))==0
    other = tmp_path/'other'
    other.mkdir()
    assert main(_args('run',other,out,dry_run=True,max_files=1))==2


def test_all_mutations_share_lock_before_touching_queue(tmp_path):
    _tone(tmp_path/'recordings'/'123456'/'a.wav')
    out = tmp_path/'out'
    assert main(_args('plan',tmp_path,out))==0
    db = sqlite3.connect(out/'state/jobs.sqlite3')
    db.execute("UPDATE jobs SET status='running',started_utc='synthetic-start'")
    db.commit()
    lock = _acquire_lock(out)
    try:
        assert _acquire_lock(out) is None
        for action in ('scan','plan','run'):
            assert main(_args(action,tmp_path,out,dry_run=True,max_files=1))==2
        assert db.execute('SELECT status,started_utc FROM jobs').fetchone()==('running','synthetic-start')
    finally:
        _release_lock(lock)
        db.close()


def test_failure_exit_code_and_half_json_timeout(tmp_path):
    source = tmp_path/'recordings'
    _tone(source/'123456'/'a.wav')
    worker = tmp_path/'error_worker.py'
    worker.write_text('import json,sys\nprint(json.dumps({"ready":True}),flush=True)\n'
                      'for line in sys.stdin:\n r=json.loads(line); '
                      'print(json.dumps({"req_id":r["req_id"],"error":"synthetic"}),flush=True)\n')
    assert main(_args('run',source,tmp_path/'failed',max_files=1,asr_python=sys.executable,
                      worker_script=str(worker)))==1
    worker.write_text('import sys,time\nsys.stdout.write("{");sys.stdout.flush();time.sleep(60)\n')
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        main(_args('run',source,tmp_path/'timeout',max_files=1,asr_python=sys.executable,
                   worker_script=str(worker),start_timeout=.05))
    assert time.monotonic()-started<5


def test_sigterm_reaps_only_owned_worker_and_cli_exit_propagates(tmp_path):
    source = tmp_path/'recordings'
    _tone(source/'123456'/'a.wav')
    out = tmp_path/'out'
    pidfile = tmp_path/'worker.pid'
    worker = tmp_path/'wait_worker.py'
    worker.write_text('import os,json,sys,time\nfrom pathlib import Path\n'
                      f'Path({str(pidfile)!r}).write_text(str(os.getpid()))\n'
                      'print(json.dumps({"ready":True}),flush=True)\n'
                      'for line in sys.stdin: time.sleep(60)\n')
    command = [sys.executable,'-m','bilibili_live_monitor','offline-asr','run','--input',str(source),
               '--output',str(out),'--max-files','1','--asr-python',sys.executable,'--worker-script',str(worker)]
    process = subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    worker_pid = None
    try:
        end = time.monotonic()+8
        while not pidfile.exists() and time.monotonic()<end:
            time.sleep(.02)
        worker_pid = int(pidfile.read_text())
        process.terminate()
        assert process.wait(timeout=8)==143
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid,0)
        lock = _acquire_lock(out)
        assert lock is not None
        _release_lock(lock)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if worker_pid:
            try:
                os.kill(worker_pid,9)
            except ProcessLookupError:
                pass
    refused = subprocess.run([sys.executable,'-m','bilibili_live_monitor','offline-asr','run',
                              '--input',str(source),'--output',str(tmp_path/'refused')],capture_output=True)
    assert refused.returncode==2


def main_capture(source, out):
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main(_args('status', source, out)) == 0
    return buffer.getvalue()


def _args(action, source, out, **extra):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    configure_parser(commands)
    argv = ['offline-asr', action, '--input', str(source), '--output', str(out)]
    if extra.get('dry_run'):
        argv.append('--dry-run')
    if extra.get('max_files'):
        argv.extend(['--max-files', str(extra['max_files'])])
    if extra.get('asr_python'):
        argv.extend(['--asr-python', extra['asr_python']])
    if extra.get('worker_script'):
        argv.extend(['--worker-script', extra['worker_script']])
    if extra.get('start_timeout'):
        argv.extend(['--start-timeout', str(extra['start_timeout'])])
    if extra.get('infer_timeout'):
        argv.extend(['--infer-timeout', str(extra['infer_timeout'])])
    return parser.parse_args(argv)


def _python():
    import sys
    return sys.executable


def _tone(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = b''.join(
        struct.pack('<h', int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
        for i in range(16000)
    )
    _wav(path, frames)


def _silence(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    _wav(path, b'\x00\x00' * 16000)


def _wav(path, frames):
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(frames)
