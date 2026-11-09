import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_recording_archive import accepted, fixture, snapshot

from bilibili_live_monitor import archive_daily as daily
from bilibili_live_monitor import recording_archive as archive


def test_dry_run_never_creates_locks_reports_or_media(tmp_path, monkeypatch):
    root, _, _ = fixture(tmp_path)
    before = snapshot(tmp_path)
    monkeypatch.setattr(archive, 'encode', lambda *_: pytest.fail('dry run encoded'))
    result = daily.run([root], trash_originals=True, report_dir=tmp_path / 'reports')
    assert result['planned'] == 1 and result['read_only']
    assert snapshot(tmp_path) == before


def test_shared_source_with_unfinished_or_conflicting_session_is_skipped(tmp_path):
    root, _, row = fixture(tmp_path)
    other = root / 'two'
    (other / 'offline').mkdir(parents=True)
    (other / 'workflow.json').write_text('{"stage":"offline_review"}')
    manifest = other / 'offline/manifest.json'
    manifest.write_text(json.dumps({'rows': [row]}))
    assert daily.plan([root])[0]['reason'] == 'referenced_by_unfinished_session'
    (other / 'workflow.json').write_text('{"stage":"completed"}')
    manifest.write_text(json.dumps({'rows': [{**row, 'sha256': 'different'}]}))
    assert daily.plan([root])[0]['reason'] == 'conflicting_source_identity'


def test_two_workers_deduplicate_roots_hold_all_locks_and_resume(tmp_path, monkeypatch):
    root, source, row = fixture(tmp_path / 'a')
    other, source2, row2 = fixture(tmp_path / 'b')
    barrier = threading.Barrier(2)
    seen = []

    def encode(item, locks):
        assert len(locks) == 2
        for directory in (root, other):
            with pytest.raises(BlockingIOError):
                with archive.writer_lock(directory):
                    pass
        barrier.wait(timeout=5)
        seen.append(item['source'])
        accepted(source if item['source'] == str(source) else source2,
                 row if item['source'] == str(source) else row2)
        return {'source': item['source'], 'status': 'archived'}

    monkeypatch.setattr(archive, 'encode', encode)
    monkeypatch.setattr(archive, 'no_writer', lambda *_: None)
    monkeypatch.setattr(archive, 'verify_output', lambda *_: {})
    # 模拟系统Trash，验证实际cleanup及其记录；不触碰用户废纸篓。
    monkeypatch.setattr(archive, 'command', lambda args: __import__('pathlib').Path(args[1]).unlink())
    reports = tmp_path / 'reports'
    result = daily.run([root, other, root], execute=True, report_dir=reports, trash_originals=True)
    assert result['complete'] and result['counts'] == {'trashed': 2}
    assert len(seen) == 2 and not source.exists() and not source2.exists()
    assert json.loads((reports / 'latest.json').read_text()) == result
    result = daily.run([root, other], execute=True, report_dir=reports, trash_originals=True)
    assert result['planned'] == 0 and result['done'] == 0


def test_per_file_failure_preserves_original_and_other_files_continue(tmp_path, monkeypatch):
    root, source, _ = fixture(tmp_path / 'a')
    other, source2, row2 = fixture(tmp_path / 'b')

    def encode(item, locks):
        if item['source'] == str(source):
            raise ValueError('unsupported')
        accepted(source2, row2)
        return {'source': str(source2), 'status': 'archived'}

    monkeypatch.setattr(archive, 'encode', encode)
    result = daily.run([root, other], execute=True, report_dir=tmp_path / 'reports')
    assert result['complete'] and result['counts'] == {'failed': 1, 'archived': 1}
    assert source.exists() and source2.exists()


def test_corrupt_archive_never_trashed(tmp_path, monkeypatch):
    root, source, row = fixture(tmp_path)
    accepted(source, row)
    monkeypatch.setattr(archive, 'no_writer', lambda *_: None)
    monkeypatch.setattr(archive, 'verify_output', lambda *_: (_ for _ in ()).throw(ValueError('broken')))
    monkeypatch.setattr(archive, 'command', lambda *_: pytest.fail('trashed corrupt archive'))
    result = daily.run([root], execute=True, report_dir=tmp_path / 'reports', trash_originals=True)
    assert result['counts'] == {'failed': 1} and source.exists()


def test_changed_shared_workflow_checked_before_cleanup(tmp_path, monkeypatch):
    root, source, row = fixture(tmp_path)
    accepted(source, row)
    item = daily.plan([root])[0]
    (root / 'one/workflow.json').write_text('{"stage":"live"}')
    monkeypatch.setattr(archive, 'command', lambda *_: pytest.fail('trashed live session'))
    result = daily.process(item, (), trash_originals=True)
    assert result['status'] == 'failed' and source.exists()


def test_overlap_does_not_launch_work(tmp_path, monkeypatch):
    root, _, _ = fixture(tmp_path)
    monkeypatch.setattr(archive, 'encode', lambda *_: pytest.fail('overlap encoded'))
    with archive.writer_lock(root), ThreadPoolExecutor() as pool:
        task = pool.submit(daily.run, [root], execute=True, report_dir=tmp_path / 'reports')
        with pytest.raises(BlockingIOError):
            task.result()
    assert not (tmp_path / 'reports').exists()
