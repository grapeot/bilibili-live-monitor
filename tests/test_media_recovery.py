import os
import shutil
import subprocess
import time

import pytest

from bilibili_live_monitor import media_recovery as recovery
from bilibili_live_monitor.offline_asr import probe


@pytest.fixture
def tail(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('需要ffmpeg')
    # 非seekable Matroska没有最终duration；与被强制打断后留下的尾片同类。
    path = tmp_path / 'tail.mkv'
    result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440',
                             '-t', '1', '-c:a', 'aac', '-f', 'matroska', '-live', '1', 'pipe:1'],
                            check=True, capture_output=True, timeout=15)
    path.write_bytes(result.stdout)
    os.utime(path, (time.time() - 400, time.time() - 400))
    row = probe(tmp_path, path)
    assert row['sealed'] == 'unsealed_or_incomplete' and row['audio_codec'] == 'aac'
    return path, row


def test_recovery_preserves_source_and_audio_packets_and_reuses_receipt(tmp_path, tail, monkeypatch):
    path, row = tail
    before = path.read_bytes(), path.stat().st_mtime_ns
    fixed = recovery.recover(row, tmp_path)
    assert fixed['sealed'] == 'sealed_readable' and fixed['duration_sec'] > 0
    assert fixed['path'] != str(path)
    assert recovery.packet_hash(path) == recovery.packet_hash(fixed['path'])
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    monkeypatch.setattr(recovery.subprocess, 'run', lambda *a, **k: pytest.fail('不得重复重封装'))
    assert recovery.recover(row, tmp_path) == fixed


def test_growing_media_and_frozen_plan_are_rejected(tmp_path, tail):
    path, row = tail
    os.utime(path, None)
    with pytest.raises(ValueError, match='素材已变化'):
        recovery.recover(row, tmp_path)
    current = probe(tmp_path, path)
    with pytest.raises(RuntimeError, match='尚未稳定'):
        recovery.recover(current, tmp_path)
    (tmp_path / 'finalize').mkdir()
    (tmp_path / 'finalize/plan.json').write_text('{}')
    with pytest.raises(ValueError, match='已冻结'):
        recovery.recover(current, tmp_path)


def test_mismatched_audio_does_not_produce_success_receipt(tmp_path, tail, monkeypatch):
    _, row = tail
    values = iter(['SHA256=' + 'a' * 64, 'SHA256=' + 'b' * 64])
    monkeypatch.setattr(recovery, 'packet_hash', lambda path: next(values))
    with pytest.raises(RuntimeError, match='不一致'):
        recovery.recover(row, tmp_path)
    assert not list((tmp_path / 'offline/recovered').glob('*.json'))


def test_non_audio_and_probe_failed_are_not_silently_recovered(tmp_path):
    for row in ({'path': '/not-used', 'sealed': 'probe_failed'},
                {'path': '/not-used', 'sealed': 'unsealed_or_incomplete', 'audio_codec': None}):
        assert recovery.recover(row, tmp_path) is row


def test_real_interrupted_ffmpeg_recording_is_recoverable(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('需要ffmpeg')
    path = tmp_path / 'interrupted.mkv'
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-re', '-f', 'lavfi', '-i', 'sine=frequency=440',
                        '-c:a', 'aac', '-cluster_time_limit', '100', '-flush_packets', '1', str(path)],
                       capture_output=True, check=True, timeout=2)
    os.utime(path, (time.time() - 400, time.time() - 400))
    row = probe(tmp_path, path)
    assert row['duration_sec'] is None and row['audio_codec'] == 'aac'
    fixed = recovery.recover(row, tmp_path)
    assert fixed['duration_sec'] > 0 and probe(tmp_path, path)['sha256'] == row['sha256']
