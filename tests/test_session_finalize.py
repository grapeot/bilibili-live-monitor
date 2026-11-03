import json
from pathlib import Path

import pytest
from test_live_share_control import initialized

from bilibili_live_monitor import session_finalize as final
from bilibili_live_monitor import session_runner as runner
from bilibili_live_monitor.live_share import atomic_json, epoch, iso


def fixture(tmp_path):
    config = initialized(tmp_path)
    start = epoch(config['start_utc'])
    rows = [{'seq': i, 'start_utc': iso(start + offset), 'end_utc': iso(start + offset + 10),
             'text': '合成讲话', 'is_final': True} for i, offset in enumerate((10, 1780, 1810, 1850))]
    workspace = Path(config['workspace'])
    config.update(offline_segments=str(workspace / 'offline_segments.json'), finalize_parallel=True)
    atomic_json(workspace / 'offline_segments.json', rows)
    atomic_json(workspace / 'state.json', {'phase': 'completed', 'cutoff': start + 3600})
    return config, rows


def results(config, plan):
    for shard in plan['shards']:
        inputs = json.loads(Path(shard['input']).read_text())
        rows = [{'seq': w['seq'], 'fingerprint': w['fingerprint'], 'label': 'speaking',
                 'music_source': 'none', 'song': None, 'investigation_version': 2}
                for w in inputs['windows'] if w['seq'] in inputs['target_seqs']]
        atomic_json(Path(shard['result']), {'plan_id': plan['id'], 'reviews': rows})


def test_shards_have_unique_ownership_and_shared_boundary_context(tmp_path):
    config, rows = fixture(tmp_path)
    plan = final.prepare(config)
    assert len(plan['shards']) == 2
    shards = [json.loads(Path(s['input']).read_text()) for s in plan['shards']]
    assert set(shards[0]['target_seqs']).isdisjoint(shards[1]['target_seqs'])
    assert sum(len(s['target_seqs']) for s in shards) == len(rows)
    assert 2 in {w['seq'] for w in shards[0]['windows']}  # 仅作邻片上下文
    assert final.prepare(config) == plan


def test_merge_preserves_followups_and_requires_every_shard(tmp_path):
    config, _ = fixture(tmp_path)
    path = Path(config['workspace']) / 'annotations.json'
    atomic_json(path, {'reviews': [], 'song_requests': [{'id': 'existing'}], 'completed_followups': []})
    plan = final.prepare(config)
    with pytest.raises(FileNotFoundError):
        final.merge(config)
    assert json.loads(path.read_text())['reviews'] == []
    results(config, plan)
    assert final.merge(config)['merged'] == 4
    assert json.loads(path.read_text())['song_requests'] == [{'id': 'existing'}]
    assert final.finish(config)['status'] == 'completed'
    assert final.complete(config)


@pytest.mark.parametrize('bad', ['identity', 'fingerprint', 'duplicate', 'context'])
def test_invalid_shard_never_partially_merges(tmp_path, bad):
    config, _ = fixture(tmp_path)
    plan = final.prepare(config)
    results(config, plan)
    path = Path(plan['shards'][0]['result'])
    data = json.loads(path.read_text())
    if bad == 'identity':
        data['plan_id'] = 'wrong'
    elif bad == 'fingerprint':
        data['reviews'][0]['fingerprint'] = 'wrong'
    elif bad == 'duplicate':
        data['reviews'][1] = data['reviews'][0]
    else:
        data['reviews'][0]['seq'] = 2
    atomic_json(path, data)
    with pytest.raises(ValueError):
        final.merge(config)
    assert json.loads((Path(config['workspace']) / 'annotations.json').read_text())['reviews'] == []


def test_idle_parent_does_not_resubmit_or_publish(tmp_path, monkeypatch):
    config, _ = fixture(tmp_path)
    state = {'stage': 'offline_review', 'final_session': 'parent'}
    submitted = []
    monkeypatch.setattr(final, 'submit', lambda *a, **k: submitted.append(a))
    runner.advance(config, state, 1)
    runner.advance(config, state, 2)
    assert len(submitted) == 1 and state['stage'] == 'offline_review'
    assert json.loads((Path(config['workspace']) / 'workflow.json').read_text())['finalize_submitted']


def test_finish_refuses_unreviewed_windows(tmp_path):
    config, _ = fixture(tmp_path)
    final.prepare(config)
    with pytest.raises(ValueError):
        final.finish(config)
    assert not final.complete(config)


def test_prepare_failure_retries_without_silent_wait(tmp_path, monkeypatch):
    config, _ = fixture(tmp_path)
    state = {'stage': 'offline_review', 'final_session': 'parent'}
    attempts = []
    def submit(*a, **kw):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError('failed before handoff')
    monkeypatch.setattr(final, 'submit', submit)
    with pytest.raises(RuntimeError):
        runner.advance(config, state, 1)
    assert not state.get('finalize_submitted') and not state.get('finalize_sending')
    runner.advance(config, state, 2)
    assert len(attempts) == 2 and state['finalize_submitted']


def test_ambiguous_submission_reconciles_without_second_handoff(tmp_path, monkeypatch):
    config, _ = fixture(tmp_path)
    state = {'stage': 'offline_review', 'final_session': 'parent'}
    attempts = []
    def submit(*a, **kw):
        attempts.append(1)
        kw['on_handoff']()
        raise RuntimeError('response lost')
    monkeypatch.setattr(final, 'submit', submit)
    with pytest.raises(RuntimeError):
        runner.advance(config, state, 1)
    monkeypatch.setattr(runner, 'ai_control', lambda *a, **k: False)
    with pytest.raises(RuntimeError, match='回执未确认'):
        runner.advance(config, state, 2)
    monkeypatch.setattr(runner, 'ai_control', lambda *a, **k: True)
    runner.advance(config, state, 3)
    assert len(attempts) == 1 and state['finalize_submitted']


def test_final_publish_rechecks_reviews_after_stopping_writers(tmp_path, monkeypatch):
    config, _ = fixture(tmp_path)
    plan = final.prepare(config)
    results(config, plan)
    final.merge(config)
    final.finish(config)
    state = {'stage': 'finalizing', 'final_session': 'parent'}
    atomic_json(Path(config['workspace']) / 'annotations.json', {'reviews': []})
    monkeypatch.setattr(runner, 'stop_ai', lambda *a, **k: True)
    monkeypatch.setattr(runner.watch, 'publish', lambda *a: pytest.fail('must not publish'))
    with pytest.raises(ValueError):
        runner.advance(config, state, 1)
    assert state['stage'] == 'finalizing'


def test_changed_frozen_input_cannot_finish(tmp_path):
    config, rows = fixture(tmp_path)
    plan = final.prepare(config)
    results(config, plan)
    final.merge(config)
    atomic_json(Path(config['offline_segments']), rows[:-1])
    with pytest.raises(ValueError, match='冻结输入'):
        final.finish(config)
    assert not final.complete(config)


def test_stop_parallel_family_waits_for_children(tmp_path, monkeypatch):
    calls = []
    def control(config, operation, **kwargs):
        calls.append((operation, kwargs['session']))
        if operation == 'children':
            return ['child'] if kwargs['session'] == 'parent' else []
        return {'type': 'busy' if kwargs['session'] == 'child' else 'idle'}
    monkeypatch.setattr(runner, 'ai_control', control)
    assert not runner.stop_ai({}, 'parent', children=True)
    assert ('abort', 'child') in calls and ('abort', 'parent') not in calls
