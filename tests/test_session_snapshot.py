import json
from pathlib import Path

import pytest
from test_live_share_control import END, initialized

from bilibili_live_monitor import live_share, live_share_watch, session_source, time_utils
from bilibili_live_monitor.live_share_control import export_snapshot
from bilibili_live_monitor.session_snapshot import build_snapshot, write_snapshot


def test_legacy_source_and_time_imports_remain_available():
    assert live_share.epoch is time_utils.epoch
    assert live_share.iso is time_utils.iso
    assert live_share.read_source is session_source.read_source
    assert live_share.scope_source is session_source.scope_source


@pytest.mark.parametrize('suffix', ['.tmp', '.html.tmp'])
def test_snapshot_output_keeps_html_before_json_and_original_temp_names(tmp_path, monkeypatch, suffix):
    config = {'start_utc': time_utils.iso(100), 'label': '合成场次', 'content_mode': 'talk'}
    snapshot = build_snapshot({'raw': [], 'items': [], 'segments': []}, config,
                              cutoff=200, phase='live', health={}, annotations={}, approved_songs=())
    replaced = []
    original = Path.replace

    def replace(path, target):
        replaced.append((path.name, Path(target).name))
        return original(path, target)

    monkeypatch.setattr(Path, 'replace', replace)
    output = tmp_path / 'public'
    write_snapshot(snapshot, output, temporary_suffix=suffix)
    assert replaced == [('index' + suffix, 'index.html'), ('snapshot.json.tmp', 'snapshot.json')]
    assert json.loads((output / 'snapshot.json').read_text()) == snapshot
    assert (output / 'index.html').read_text() == live_share.render_snapshot(snapshot)
    assert snapshot['content_mode'] == 'talk' and snapshot['activity'] == []


@pytest.mark.parametrize('failure', ['index.html.tmp', 'snapshot.json.tmp'])
def test_failed_write_keeps_existing_failure_order(tmp_path, monkeypatch, failure):
    snapshot = build_snapshot({'raw': [], 'items': [], 'segments': []},
                              {'start_utc': time_utils.iso(100), 'label': '合成场次'},
                              cutoff=200, phase='completed', health={}, annotations={}, approved_songs=())
    output = tmp_path / 'public'
    output.mkdir()
    (output / 'index.html').write_text('old page')
    (output / 'snapshot.json').write_text('{}')
    original = Path.write_text

    def write(path, *args, **kwargs):
        if path.name == failure:
            raise OSError('synthetic write failure')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)
    with pytest.raises(OSError, match='synthetic'):
        write_snapshot(snapshot, output, temporary_suffix='.html.tmp')
    assert (output / 'snapshot.json').read_text() == '{}'
    expected = 'old page' if failure == 'index.html.tmp' else live_share.render_snapshot(snapshot)
    assert (output / 'index.html').read_text() == expected


def test_manual_export_and_workflow_watch_keep_distinct_stop_phases(tmp_path):
    config = initialized(tmp_path)
    config['workflow_key'] = 'synthetic-workflow'
    root = Path(config['workspace'])
    state = {'stop_intent': True, 'phase': 'live', 'cutoff': time_utils.epoch(END)}
    live_share.atomic_json(root / 'state.json', state)
    assert export_snapshot(config)['phase'] == 'completed'
    api = {'rooms': [{'room_id': '123456', 'recorded_seconds': 0, 'recording_bytes': 0,
                      'recording_status': 'disabled', 'asr_enabled': False}],
           'asr_ready': False, 'queue_depth': 0}
    live_share_watch.build(config, state, {}, api, time_utils.epoch(END), upload=False)
    saved = json.loads((root / 'public/snapshot.json').read_text())
    assert saved['phase'] == 'finalizing'
    assert saved['health']['recording'] is False
