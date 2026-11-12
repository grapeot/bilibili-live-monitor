import copy
import json
import subprocess
from pathlib import Path

import pytest
from test_audience_crm import config as crm_config
from test_audience_crm import session as material_session
from test_audience_crm import state_for
from test_live_share_control import initialized

from bilibili_live_monitor import audience_crm as crm
from bilibili_live_monitor import audience_finalize as audience
from bilibili_live_monitor import cursor_finalize as cursor
from bilibili_live_monitor.live_share import atomic_json


def make(tmp_path):
    session = initialized(tmp_path)
    cfg = crm_config(tmp_path, room=session['room_id'])
    cfg.update(author_agent=audience.CURSOR_AUTHOR, coordinator_model='openai/gpt-6.1-sol')
    material = crm.collect(cfg, [material_session(room=cfg['room_id'])])
    directory, plan = crm.prepare(cfg, material)
    marker = Path(session['workspace']) / 'audience_finalize'
    marker.mkdir()
    atomic_json(marker / 'plan.json', {'run': str(directory), 'plan_id': plan['id']})
    return session, cfg, directory, plan, material


def test_music_dispatch_never_reinterprets_audience_marker(tmp_path):
    session, _, _, _, _ = make(tmp_path)
    session.update(final_subagent=cursor.BACKEND, model='openai/gpt-6.1-sol')
    root = Path(session['workspace']) / 'audience_finalize'
    assert cursor.coordinator_prompt(session, 'audience contract', root) == 'audience contract'


def test_audience_cursor_dispatch_preserves_gpt_coordinator_and_source_contract(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_finalize, session_runner
    session, cfg, directory, _, _ = make(tmp_path)
    settings = tmp_path / 'crm.json'
    atomic_json(settings, cfg)
    session.update(audience_crm_config=str(settings), final_subagent=cursor.BACKEND)
    monkeypatch.setattr(session_runner, 'ensure_ai', lambda *a: 'coordinator')
    calls = []
    def dispatch(*args):
        calls.append(args)
        args[-1]()
    monkeypatch.setattr(session_finalize, 'dispatch', dispatch)
    state = {}
    assert not audience.advance(session, state)
    assert not audience.advance(session, state)
    assert len(calls) == 1 and calls[0][0]['model'] == 'openai/gpt-6.1-sol'
    prompt = calls[0][2]
    assert '使用task的cursor_grok_cli' not in prompt
    assert 'cursor_finalize start' in prompt and '--kind audience_finalize' in prompt
    assert 'PYTHONPATH=' in prompt and 'previous' in prompt and 'source_manifest' in prompt
    assert '最多10并发' in prompt and 'audience_crm finish' in prompt
    assert str(directory) in prompt and state['audience_submitted']


@pytest.mark.parametrize('bad', [None, 'quote', 'revision'])
def test_cursor_person_candidate_requires_success_and_original_crm_validation(tmp_path, monkeypatch, bad):
    session, cfg, directory, plan, material = make(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    receipt = cursor.launch(session, 'audience_finalize', '000')
    task = plan['tasks'][0]
    payload = copy.deepcopy(state_for(material['people'][task['person_id']]))
    if bad == 'quote':
        payload['overview'][0]['refs'][0]['quote'] = '不存在的引文'
    if bad == 'revision':
        payload['revision'] = 'wrong'
    class Process:
        pid = 1234
        def __init__(self, command, **kwargs):
            assert command[command.index('--model') + 1] == 'grok-4.7-high'
            assert kwargs['stdin'] == subprocess.DEVNULL and kwargs['start_new_session']
            events = [{'type': 'system', 'subtype': 'init', 'session_id': 'person-cli',
                       'model': 'Grok 4.7 256K High'},
                      {'type': 'result', 'subtype': 'success', 'is_error': False}]
            kwargs['stdout'].write(('\n'.join(json.dumps(e) for e in events) + '\n').encode())
            atomic_json(directory / 'cursor_workers/000.result.json', payload)
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(cursor.subprocess, 'Popen', Process)
    monkeypatch.setattr(cursor, 'stop_group', lambda p: None)
    assert cursor.worker(session, 'audience_finalize', '000', receipt['token'])['state'] == 'awaiting_coordinator_review'
    worker_prompt = (directory / 'cursor_workers/000.prompt.md').read_text()
    assert 'instructions.txt' in worker_prompt and '不得搜索互联网' in worker_prompt
    assert not Path(task['result']).exists()
    with pytest.raises(ValueError, match='不能finish'):
        crm.finish(cfg, directory)
    if bad:
        with pytest.raises(ValueError):
            cursor.accept(session, 'audience_finalize', '000')
        assert not Path(task['result']).exists()
        assert not (Path(cfg['workspace']) / 'current.json').exists()
    else:
        assert cursor.accept(session, 'audience_finalize', '000')['state'] == 'accepted'
        assert crm.finish(cfg, directory)['status'] == 'completed'


def test_legacy_person_result_is_validated_without_faking_cli_or_restarting(tmp_path, monkeypatch):
    session, cfg, directory, plan, material = make(tmp_path)
    task = plan['tasks'][0]
    atomic_json(Path(task['result']), state_for(material['people'][task['person_id']]))
    monkeypatch.setattr(cursor, 'post', lambda *a: pytest.fail('must not relaunch old result'))
    assert cursor.launch(session, 'audience_finalize', '000')['state'] == 'existing_result_requires_coordinator_review'
    state = cursor.accept(session, 'audience_finalize', '000')
    assert state['backend'] == 'legacy_result' and 'exit_code' not in state
    assert crm.finish(cfg, directory)['status'] == 'completed'


def test_audience_stop_fences_its_run_without_stopping_music(tmp_path, monkeypatch):
    session, _, directory, _, _ = make(tmp_path)
    music = Path(session['workspace']) / 'finalize'
    music.mkdir()
    assert cursor.stop_workers(session, kinds=('audience_finalize',))
    assert (directory / 'cursor_workers/stop.json').exists()
    assert not (music / 'cursor_workers').exists()
    monkeypatch.setattr(cursor, 'post', lambda *a: pytest.fail('late dispatch after stop'))
    with pytest.raises(ValueError, match='已停止派发'):
        cursor.launch(session, 'audience_finalize', '000')
