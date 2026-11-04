import json

import pytest

from bilibili_live_monitor import session_backfill as backfill
from bilibili_live_monitor.live_share import iso
from bilibili_live_monitor.live_share_control import prefer_offline, read_session_source


def segment(seq, start, end, text='synthetic'):
    return {'seq': seq, 'start_utc': iso(start), 'end_utc': iso(end), 'text': text, 'is_final': True}


def test_offline_priority_keeps_uncovered_holes_and_boundary_evidence():
    live = [segment(1, 0, 10), segment(2, 10, 20), segment(3, 20, 30),
            segment(4, 30, 40), segment(5, 40, 50)]
    offline = [segment(-1, 0, 7), segment(-2, 7, 15), segment(-3, 30, 40)]
    merged = prefer_offline(live, offline)
    assert {s['seq'] for s in merged} == {-1, -2, -3, 2, 3, 5}
    assert prefer_offline(live, []) == live


def test_final_offline_source_still_overrides_live_backfill(tmp_path, monkeypatch):
    import bilibili_live_monitor.live_share_control as control
    live, offline = [segment(1, 0, 10)], [segment(-1, 0, 10)]
    monkeypatch.setattr(control, 'read_source', lambda *a: {'segments': live})
    (tmp_path / 'live_backfill_segments.json').write_text(json.dumps(offline))
    cfg = {'workspace': str(tmp_path), 'database': 'synthetic', 'room_id': '123456', 'start_utc': iso(0)}
    assert read_session_source(cfg)['segments'] == offline
    (tmp_path / 'final.json').write_text(json.dumps([segment(-2, 0, 10)]))
    assert read_session_source({**cfg, 'offline_segments': str(tmp_path / 'final.json')})['segments'][0]['seq'] == -2


def test_backfill_plan_skips_open_file_and_keeps_missing_index_offset(tmp_path, monkeypatch):
    sealed = tmp_path / 'sealed.mkv'
    opened = tmp_path / 'open.mkv'
    sealed.write_bytes(b'sealed')
    opened.write_bytes(b'open')
    monkeypatch.setattr(backfill, 'recording_files', lambda *a: {(100, 'group'): [(2, sealed), (3, opened)]})
    cutoff = sealed.stat().st_mtime + 20
    import os
    os.utime(opened, (cutoff, cutoff))
    monkeypatch.setattr(backfill.offline_asr, 'probe', lambda root, path:
                        {'path': str(path), 'duration_sec': 300, 'sealed': 'sealed_readable'})
    plan = backfill.prepare({'recording_root': str(tmp_path), 'recording_segment_seconds': 300,
                             'room_id': '123456'}, cutoff)
    assert plan['rows'][0]['wall_start'] == 700
    assert plan['skipped'][0]['reason'] == 'recent_write'


def test_dry_run_does_not_mutate_workflow_or_publish(tmp_path, monkeypatch):
    (tmp_path / 'workflow.json').write_text('{"stage":"live"}')
    monkeypatch.setattr(backfill, 'prepare', lambda *a: {'cutoff': 1000, 'rows': [], 'skipped': []})
    result = backfill.run({'workspace': str(tmp_path)}, dry_run=True)
    assert result['dry_run']
    assert not (tmp_path / 'live_backfill/manifest.json').exists()
    assert json.loads((tmp_path / 'workflow.json').read_text()) == {'stage': 'live'}


def test_empty_plan_does_not_freeze_future_scan(tmp_path, monkeypatch):
    calls = []
    def prepare(config, cutoff):
        calls.append(cutoff)
        return {'cutoff': cutoff, 'rows': [] if len(calls) == 1 else [{'duration_sec': 5}], 'skipped': []}
    monkeypatch.setattr(backfill, 'prepare', prepare)
    with pytest.raises(RuntimeError, match='封口'):
        backfill.run({'workspace': str(tmp_path)}, cutoff=1000)
    assert not (tmp_path / 'live_backfill/manifest.json').exists()
    assert backfill.run({'workspace': str(tmp_path)}, cutoff=1300, dry_run=True)['files'] == 1
    assert calls == [1000, 1300]
