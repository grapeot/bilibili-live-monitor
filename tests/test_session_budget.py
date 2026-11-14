import json

import pytest

from bilibili_live_monitor import cursor_finalize as cursor
from bilibili_live_monitor import session_finalize, session_health, session_runner
from bilibili_live_monitor.live_share import atomic_json
from bilibili_live_monitor.session_budget import BudgetPaused, check_budget


@pytest.mark.parametrize('stage', ['stopping', 'sealing', 'offline_asr', 'offline_review', 'finalizing'])
def test_expired_runner_persists_pause_without_cleanup_or_publication(tmp_path, monkeypatch, stage):
    config = {'workspace': str(tmp_path), 'ai_session': 'periodic'}
    original = {'stage': stage, 'finish_deadline': 0, 'final_session': 'coordinator',
                'audience_session': 'audience', 'finalize_submitted': True}
    atomic_json(tmp_path / 'workflow.json', original)
    monkeypatch.setattr(session_runner, 'load_config', lambda p: config)
    def forbidden(*a, **kw):
        pytest.fail('预算暂停不得推进、清理、发布或新建巡检')
    for module, name in ((session_runner, 'advance'), (session_runner, 'stop_ai'),
                         (session_runner, 'mark_attention'), (cursor, 'stop_workers'),
                         (session_health, 'ensure_scheduled')):
        monkeypatch.setattr(module, name, forbidden)
    assert session_runner.run(tmp_path / 'config.json') == 1
    saved = json.loads((tmp_path / 'workflow.json').read_text())
    assert saved['stage'] == 'attention'
    assert saved['automation_paused']['stage'] == stage
    for field in ('finish_deadline', 'final_session', 'audience_session', 'finalize_submitted'):
        assert saved[field] == original[field]
    assert session_runner.run(tmp_path / 'config.json') == 1
    assert json.loads((tmp_path / 'workflow.json').read_text())['automation_paused'] == saved['automation_paused']
    assert session_health.run(config, reschedule=True)['stopped']


@pytest.mark.parametrize('operation', ['launch', 'retry', 'dispatch', 'ensure_ai'])
def test_expired_budget_blocks_dispatch_even_before_runner_observes_it(tmp_path, monkeypatch, operation):
    config = {'workspace': str(tmp_path)}
    atomic_json(tmp_path / 'workflow.json', {'stage': 'offline_review', 'finish_deadline': 0})
    def forbidden(*a, **kw):
        pytest.fail('不得创建会话、派发进程或修改重试账本')
    monkeypatch.setattr(cursor, 'context', forbidden)
    monkeypatch.setattr(session_runner, 'ai_control', forbidden)
    monkeypatch.setattr(session_finalize.subprocess, 'run', forbidden)
    with pytest.raises(BudgetPaused):
        if operation in ('launch', 'retry'):
            getattr(cursor, operation)(config, 'finalize', '000')
        elif operation == 'dispatch':
            session_finalize.dispatch(config, 'coordinator', 'prompt', tmp_path, forbidden)
        else:
            session_runner.ensure_ai(config, 'finalize')


def test_pause_requires_explicit_manual_clear_not_just_longer_deadline(tmp_path):
    config = {'workspace': str(tmp_path)}
    atomic_json(tmp_path / 'workflow.json', {'automation_paused': {'reason': 'finish_timeout'},
                                           'finish_deadline': 99999999999})
    with pytest.raises(BudgetPaused):
        check_budget(config)
    check_budget(config, {'finish_deadline': 100}, now=99)
    with pytest.raises(BudgetPaused):
        check_budget(config, {'finish_deadline': 100}, now=100)


def test_expiration_inside_advance_pauses_without_error_cleanup(tmp_path, monkeypatch):
    config = {'workspace': str(tmp_path)}
    atomic_json(tmp_path / 'workflow.json', {'stage': 'finalizing'})
    monkeypatch.setattr(session_runner, 'load_config', lambda p: config)
    monkeypatch.setattr(session_health, 'ensure_scheduled', lambda *a, **kw: None)
    def advance(*a):
        raise BudgetPaused('expired at publication boundary')
    monkeypatch.setattr(session_runner, 'advance', advance)
    monkeypatch.setattr(session_runner, 'stop_ai', lambda *a, **kw: pytest.fail('不应停止会话'))
    assert session_runner.run(tmp_path / 'config.json') == 1
    assert json.loads((tmp_path / 'workflow.json').read_text())['automation_paused']['stage'] == 'finalizing'


def test_unrelated_errors_keep_existing_cleanup(tmp_path, monkeypatch):
    config = {'workspace': str(tmp_path), 'ai_session': 'periodic'}
    monkeypatch.setattr(session_runner, 'load_config', lambda p: config)
    monkeypatch.setattr(session_health, 'ensure_scheduled', lambda *a, **kw: None)
    def advance(*a):
        raise ValueError('invalid media')
    monkeypatch.setattr(session_runner, 'advance', advance)
    stopped = []
    monkeypatch.setattr(session_runner, 'stop_ai', lambda c, s, **kw: stopped.append(s) or True)
    monkeypatch.setattr(cursor, 'stop_workers', lambda c: stopped.append('workers') or True)
    monkeypatch.setattr(session_runner, 'mark_attention', lambda c: stopped.append('attention'))
    assert session_runner.run(tmp_path / 'config.json') == 1
    assert stopped == ['periodic', 'workers', 'attention']
    assert 'automation_paused' not in json.loads((tmp_path / 'workflow.json').read_text())


def test_independent_audience_budget_used_by_helpers_and_cursor_cli(tmp_path, monkeypatch):
    config = {'workspace': str(tmp_path), 'label': '示例', 'workflow_key': 'example',
              'opencode_project': '/synthetic'}
    atomic_json(tmp_path / 'workflow.json', {'stage': 'completed', 'finish_deadline': 0})
    state_path = tmp_path / 'audience.state.json'
    atomic_json(state_path, {'finish_deadline': 99999999999})
    marker = tmp_path / 'audience_finalize' / 'plan.json'
    marker.parent.mkdir()
    atomic_json(marker, {'budget_state_path': str(state_path)})
    owned = {**config, 'audience_state_path': str(state_path)}
    monkeypatch.setattr(session_runner, 'ai_control', lambda *a, **kw: {'id': 'new-audience'})
    assert session_runner.ensure_ai(owned, 'audience_finalize') == 'new-audience'
    check_budget(config, kind='audience_finalize')
    atomic_json(state_path, {'finish_deadline': 0})
    with pytest.raises(BudgetPaused):
        session_runner.ensure_ai(owned, 'audience_finalize')
    for operation in (cursor.launch, cursor.retry):
        with pytest.raises(BudgetPaused):
            operation(config, 'audience_finalize', '000')
