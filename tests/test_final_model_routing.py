from pathlib import Path
from subprocess import CompletedProcess

import pytest
from test_session_finalize import fixture

from bilibili_live_monitor import session_finalize as final
from bilibili_live_monitor import session_runner as runner
from bilibili_live_monitor.live_share import epoch


@pytest.mark.parametrize('separate', [False, True])
def test_parallel_final_dispatch_uses_final_model_without_changing_realtime(tmp_path, monkeypatch, separate):
    config, _ = fixture(tmp_path)
    config.update(model='realtime-provider/realtime-model', opencode_python='/synthetic/python',
                  opencode_cwd=str(tmp_path), final_subagent='synthetic-final-agent', final_concurrency=10)
    if separate:
        config['final_model'] = 'final-provider/final-model'
    calls = []
    monkeypatch.setattr(final.subprocess, 'run', lambda command, **kwargs:
                        calls.append(command) or CompletedProcess(command, 0, stdout='{}', stderr=''))
    final.submit(config, 'synthetic-final-session')
    command = calls[0]
    assert command[command.index('--model') + 1] == config.get('final_model', config['model'])
    assert config['model'] == 'realtime-provider/realtime-model'
    prompt = (Path(config['workspace']) / 'finalize/prompt.md').read_text()
    assert 'synthetic-final-agent subagent' in prompt and '最多同时10片' in prompt


def test_serial_final_dispatch_uses_final_model_without_changing_realtime(tmp_path, monkeypatch):
    config, _ = fixture(tmp_path)
    config.update(model='realtime-provider/realtime-model', final_model='final-provider/final-model',
                  finalize_parallel=False, ai_enabled=True, ai_session='synthetic-realtime')
    calls = []
    monkeypatch.setattr(runner, 'ai_control', lambda *args, **kwargs: {'type': 'idle'})
    monkeypatch.setattr(runner.watch, 'ai_once', lambda cfg, *args, **kwargs: calls.append((cfg, kwargs)))
    state = {'stage': 'offline_review', 'final_session': 'synthetic-final',
             'cutoff': epoch(config['start_utc']) + 3600}
    runner.advance(config, state, 1)
    assert calls[0][0]['model'] == 'final-provider/final-model'
    assert calls[0][0]['ai_session'] == 'synthetic-final'
    assert calls[0][1]['final'] is True
    assert config['model'] == 'realtime-provider/realtime-model'
    assert config['ai_session'] == 'synthetic-realtime'


def test_shared_dispatch_preserves_explicit_crm_coordinator(tmp_path, monkeypatch):
    config = {'workspace': str(tmp_path),
              'model': 'crm-provider/coordinator', 'final_model': 'final-provider/final-model',
              'opencode_python': '/synthetic/python', 'opencode_cwd': str(tmp_path)}
    calls = []
    monkeypatch.setattr(final.subprocess, 'run', lambda command, **kwargs:
                        calls.append(command) or CompletedProcess(command, 0, stdout='{}', stderr=''))
    final.dispatch(config, 'synthetic-crm-session', 'synthetic prompt', tmp_path, lambda: None)
    command = calls[0]
    assert command[command.index('--model') + 1] == 'crm-provider/coordinator'
