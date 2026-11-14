import errno
import json
import subprocess
from pathlib import Path

import pytest
from test_session_finalize import fixture

from bilibili_live_monitor import cursor_finalize as cursor
from bilibili_live_monitor import session_finalize as music
from bilibili_live_monitor import talk_finalize as talk
from bilibili_live_monitor.live_share import atomic_json
from bilibili_live_monitor.session_workflow import validate_settings


def setup(tmp_path, kind='finalize'):
    config, _ = fixture(tmp_path)
    config.update(final_subagent=cursor.BACKEND, final_model='xai/grok-4.7',
                  model='xai/grok-4.7')
    plan = (music if kind == 'finalize' else talk).prepare(config)
    root = Path(config['workspace']) / kind
    (root / 'prompt.md').write_text('总协调合同')
    return config, plan, root


def test_explicit_final_model_prevents_realtime_fallback():
    with pytest.raises(ValueError, match='final_model'):
        validate_settings({'final_subagent': cursor.BACKEND, 'model': 'qwen/local'}, {})
    validate_settings({'final_subagent': cursor.BACKEND, 'final_model': 'xai/grok-4.7'}, {})
    validate_settings({'final_subagent': cursor.BACKEND,
                       'final_model': 'example/coordinator'}, {})


@pytest.mark.parametrize('kind', ['finalize', 'talk_finalize'])
def test_coordinator_model_is_independent_of_cursor_worker(tmp_path, monkeypatch, kind):
    config, _, root = setup(tmp_path, kind)
    config.update(model='example/realtime', final_model='example/coordinator')
    sent = []
    monkeypatch.setattr(music.subprocess, 'run', lambda command, **kw:
        sent.append(command) or subprocess.CompletedProcess(command, 0, '', ''))
    (music if kind == 'finalize' else talk).submit(config, 'coordinator')
    assert sent[-1][sent[-1].index('--model') + 1] == 'example/coordinator'
    assert 'cursor_finalize start' in (root / 'prompt.md').read_text()
    assert cursor.MODEL == 'grok-4.7-high'


@pytest.mark.parametrize('final_model', [None, '', 'invalid', '/missing', 'missing/', ' example/model'])
def test_cursor_requires_valid_explicit_coordinator(tmp_path, final_model):
    config, _, root = setup(tmp_path)
    config['final_model'] = final_model
    with pytest.raises(ValueError, match='final_model'):
        validate_settings(config, {})
    with pytest.raises(ValueError, match='final_model'):
        cursor.coordinator_prompt(config, 'contract', root)


def test_cursor_rejects_effective_realtime_model(tmp_path):
    config, _, root = setup(tmp_path)
    config['model'] = 'example/realtime'
    with pytest.raises(ValueError, match='final_model'):
        cursor.coordinator_prompt(config, 'contract', root)


def test_music_and_talk_dispatch_preserve_content_contract(tmp_path, monkeypatch):
    config, _, root = setup(tmp_path)
    sent = []
    monkeypatch.setattr(music.subprocess, 'run', lambda command, **kw:
        sent.append(command) or subprocess.CompletedProcess(command, 0, '', ''))
    music.submit(config, 'coordinator')
    prompt = (root / 'prompt.md').read_text()
    assert '使用task并行派发' not in prompt
    assert '子会话必须由本会话task' not in prompt
    assert 'cursor_finalize start' in prompt and 'accept操作' in prompt
    assert 'transcript_corrections.json' in prompt and 'correction_id' in prompt
    assert sent[-1][sent[-1].index('--model') + 1] == 'xai/grok-4.7'
    config['content_mode'] = 'talk'
    talk.submit(config, 'talk-coordinator')
    prompt = (root.parent / 'talk_finalize/prompt.md').read_text()
    assert '再用task分发' not in prompt
    assert '禁止联网搜索' in prompt and '--kind talk_finalize' in prompt


def test_launch_is_idempotent_and_caps_concurrency(tmp_path, monkeypatch):
    config, plan, root = setup(tmp_path)
    plan['concurrency'] = 1
    atomic_json(root / 'plan.json', plan)
    calls = []
    monkeypatch.setattr(cursor, 'post', lambda c, p:
        calls.append(p) or {'pid': 123, 'diagnose_on_failure': False})
    first = cursor.launch(config, 'finalize', '000')
    assert first['launcher']['pid'] == 123
    assert cursor.launch(config, 'finalize', '000')['token'] == first['token']
    assert len(calls) == 1 and calls[0]['timeout'] > 2700
    with pytest.raises(ValueError, match='并发已满'):
        cursor.launch(config, 'finalize', '001')
    with pytest.raises(ValueError, match='不属于'):
        cursor.launch(config, 'finalize', '../../escape')


def test_ambiguous_launch_never_reposts(tmp_path, monkeypatch):
    config, _, root = setup(tmp_path)
    calls = []
    def ambiguous(*a):
        calls.append(1)
        raise TimeoutError('response lost')
    monkeypatch.setattr(cursor, 'post', ambiguous)
    with pytest.raises(TimeoutError):
        cursor.launch(config, 'finalize', '000')
    assert cursor.launch(config, 'finalize', '000')['state'] == 'submitting'
    assert len(calls) == 1
    assert json.loads((root / 'cursor_workers/000.launch_error.json').read_text())['ambiguous']


@pytest.mark.parametrize('kind', ['finalize', 'talk_finalize'])
@pytest.mark.parametrize('failure', [None, 'exit', 'no_result', 'model', 'coverage', 'fingerprint'])
@pytest.mark.parametrize('paused', [False, True])
def test_owned_worker_receipt_and_acceptance(tmp_path, monkeypatch, kind, failure, paused):
    config, plan, root = setup(tmp_path, kind)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, kind, '000')
    if paused:
        atomic_json(Path(config['workspace']) / 'workflow.json', {
            'stage': 'attention', 'finish_deadline': 0,
            'automation_paused': {'reason': 'finish_timeout'}})
    shard = plan['shards'][0]
    inputs = json.loads(Path(shard['input']).read_text())
    rows = []
    for w in inputs['windows']:
        if w['seq'] not in inputs['target_seqs']:
            continue
        row = {'seq': w['seq'], 'fingerprint': w['fingerprint'], 'confidence': 'high',
               'label': 'speaking', 'music_source': 'none', 'song': None, 'investigation_version': 2,
               'corrected_text': w['text'], 'reason': '保留原稿', 'evidence': []}
        rows.append(row)
    if failure == 'coverage':
        rows.pop()
    if failure == 'fingerprint':
        rows[0]['fingerprint'] = 'wrong'
    class Process:
        pid = 1234
        def __init__(self, command, **kwargs):
            assert command[0:3] == ['cursor', 'agent', '-p']
            assert command[command.index('--model') + 1] == cursor.MODEL
            assert kwargs['stdin'] == subprocess.DEVNULL and kwargs['start_new_session']
            events = [{'type': 'system', 'subtype': 'init', 'session_id': 'cli-id',
                       'model': 'Wrong Model' if failure == 'model' else 'Grok 4.7 256K High'}]
            if failure != 'no_result':
                events.append({'type': 'result', 'subtype': 'success', 'is_error': False,
                               'session_id': 'cli-id'})
            kwargs['stdout'].write(('\n'.join(json.dumps(e) for e in events) + '\n').encode())
            atomic_json(root / 'cursor_workers/000.result.json', {'plan_id': plan['id'], 'reviews': rows})
        def wait(self, timeout):
            assert timeout == 2700  # 整场自动推进暂停不改变已派发单片的45分钟上限。
            return 1 if failure == 'exit' else 0
    monkeypatch.setattr(cursor.subprocess, 'Popen', Process)
    monkeypatch.setattr(cursor, 'stop_group', lambda p: None)
    result = cursor.worker(config, kind, '000', state['token'])
    assert result['session_ids'] == ['cli-id']
    assert not Path(shard['result']).exists()  # worker不得转正。
    with pytest.raises(ValueError, match='不能finish'):
        cursor.require_stopped(config, kind)
    if failure:
        with pytest.raises(ValueError):
            cursor.accept(config, kind, '000')
        assert not Path(shard['result']).exists()
    else:
        assert result['state'] == 'awaiting_coordinator_review'
        assert cursor.accept(config, kind, '000')['state'] == 'accepted'
        cursor.require_stopped(config, kind)
        assert json.loads(Path(shard['result']).read_text())['reviews'] == rows


def test_start_failure_records_terminal_state_and_releases_slot(tmp_path, monkeypatch):
    config, _, root = setup(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    def missing(*a, **k):
        raise FileNotFoundError('cursor')
    monkeypatch.setattr(cursor.subprocess, 'Popen', missing)
    assert cursor.worker(config, 'finalize', '000', state['token'])['state'] == 'failed'
    assert json.loads((root / 'cursor_workers/000.json').read_text())['error'] == 'FileNotFoundError'


def test_stop_group_escalates_even_after_leader_exits(monkeypatch):
    signals = []
    class Process:
        pid = 1234
        def wait(self, timeout):
            return -15
    monkeypatch.setattr(cursor.os, 'killpg', lambda pid, sig: signals.append((pid, sig)))
    cursor.stop_group(Process())
    assert signals == [(1234, cursor.signal.SIGTERM), (1234, cursor.signal.SIGKILL)]


def test_runner_stop_targets_only_own_adapter_then_waits_for_receipt(tmp_path, monkeypatch):
    from bilibili_live_monitor import live_share_watch as watch
    config, _, root = setup(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    stopped = []
    def request(url, payload=None):
        if payload is not None:
            stopped.append(url)
            return {}
        return [{'pid': 123, 'status': 'running', 'command':
                 ['python', '-m', 'bilibili_live_monitor.cursor_finalize', '--token', state['token'],
                  '--config', config['_config_path']]},
                {'pid': 999, 'status': 'running', 'command': ['python', '--token', 'other']}]
    monkeypatch.setattr(watch, 'request', request)
    assert not cursor.stop_workers(config)
    assert len(stopped) == 1 and stopped[0].endswith('/123/stop')
    state['state'] = 'interrupted'
    atomic_json(root / 'cursor_workers/000.json', state)
    assert cursor.stop_workers(config)


def test_proven_unsent_launcher_failure_is_terminal(tmp_path, monkeypatch):
    config, _, root = setup(tmp_path)
    def refused(*a):
        raise ConnectionRefusedError(errno.ECONNREFUSED, 'refused')
    monkeypatch.setattr(cursor, 'post', refused)
    with pytest.raises(ConnectionRefusedError):
        cursor.launch(config, 'finalize', '000')
    assert cursor.launch(config, 'finalize', '000')['state'] == 'failed'
    assert cursor.stop_workers(config)


def test_ambiguous_not_started_cancels_with_fence(tmp_path, monkeypatch):
    from bilibili_live_monitor import live_share_watch as watch
    config, _, root = setup(tmp_path)
    def ambiguous(*a):
        raise TimeoutError('lost')
    monkeypatch.setattr(cursor, 'post', ambiguous)
    with pytest.raises(TimeoutError):
        cursor.launch(config, 'finalize', '000')
    monkeypatch.setattr(watch, 'request', lambda *a: [])
    assert cursor.stop_workers(config)
    assert json.loads((root / 'cursor_workers/000.json').read_text())['state'] == 'cancelled_before_start'
    with pytest.raises(ValueError, match='已停止派发'):
        cursor.launch(config, 'finalize', '001')


def test_legacy_result_acceptance_does_not_forge_cursor_receipt(tmp_path):
    from test_session_finalize import results
    config, plan, root = setup(tmp_path)
    results(config, plan)
    for shard in plan['shards']:
        shard_id = Path(shard['input']).name.split('.')[0]
        assert cursor.launch(config, 'finalize', shard_id)['state'] == 'existing_result_requires_coordinator_review'
        receipt = cursor.accept(config, 'finalize', shard_id)
        assert receipt['backend'] == 'legacy_result'
        assert 'exit_code' not in receipt and 'session_ids' not in receipt
    assert music.merge(config)['merged'] == 4
    assert music.finish(config)['status'] == 'completed'


def test_stop_before_first_dispatch_fences_late_start(tmp_path, monkeypatch):
    config, _, root = setup(tmp_path)
    calls = []
    monkeypatch.setattr(cursor, 'post', lambda *a: calls.append(a))
    assert cursor.stop_workers(config)
    assert (root / 'cursor_workers/stop.json').exists()
    with pytest.raises(ValueError, match='已停止派发'):
        cursor.launch(config, 'finalize', '000')
    assert not calls


def test_retry_preserves_logs_resumes_same_session_and_is_bounded(tmp_path, monkeypatch):
    config, _, root = setup(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    runs = root / 'cursor_workers'
    for attempt in range(2):
        token = state['token']
        state['state'] = 'failed'
        atomic_json(runs / '000.json', state)
        (runs / '000.stdout.jsonl').write_text(json.dumps({'type': 'system', 'session_id': 'same-cli'}))
        state = cursor.retry(config, 'finalize', '000')
        assert state['resume_session'] == 'same-cli' and state['token'] != token
        assert (runs / 'history/000' / token / '000.stdout.jsonl').exists()
    state['state'] = 'failed'
    atomic_json(runs / '000.json', state)
    with pytest.raises(ValueError, match='恢复上限'):
        cursor.retry(config, 'finalize', '000')


def test_retry_refuses_a_live_prior_process_group(tmp_path, monkeypatch):
    config, _, root = setup(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    state.update(state='failed', pgid=100)
    atomic_json(root / 'cursor_workers/000.json', state)
    monkeypatch.setattr(cursor.os, 'killpg', lambda *a: None)
    with pytest.raises(ValueError, match='进程组仍存在'):
        cursor.retry(config, 'finalize', '000')


@pytest.mark.parametrize('members,raises', [('', False), ('1234\n', True)])
def test_permission_error_only_ignored_for_proven_empty_group(monkeypatch, members, raises):
    from types import SimpleNamespace
    def denied(*args):
        raise PermissionError('denied')
    monkeypatch.setattr(cursor.os, 'killpg', denied)
    monkeypatch.setattr(cursor.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=members))
    process = SimpleNamespace(pid=1234, wait=lambda **kw: 0)
    if raises:
        with pytest.raises(PermissionError):
            cursor.stop_group(process)
    else:
        cursor.stop_group(process)


@pytest.mark.parametrize('mismatch,live', [(False, False), (True, False), (False, True)])
def test_orphan_reconciliation_requires_identity_and_no_group(tmp_path, monkeypatch, mismatch, live):
    from bilibili_live_monitor import live_share_watch as watch
    config, _, root = setup(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    state.update(state='running', adapter_pid=123, pgid=456)
    path = root / 'cursor_workers/000.json'
    atomic_json(path, state)
    job = {'pid': 123, 'status': 'exited', 'exit_code': 1, 'exited_at': '2030-01-01',
           'command': [cursor.sys.executable, '-m', 'bilibili_live_monitor.cursor_finalize', 'worker',
                       '--config', config['_config_path'], '--kind', 'finalize',
                       '--shard', '000', '--token', 'other' if mismatch else state['token']]}
    monkeypatch.setattr(watch, 'request', lambda *a: job)
    monkeypatch.setattr(cursor, 'group_exists', lambda *a: live)
    if mismatch:
        with pytest.raises(ValueError, match='身份'):
            cursor.reconcile_worker(config, 'finalize', '000')
    else:
        assert cursor.reconcile_worker(config, 'finalize', '000') == (not live)
    saved = json.loads(path.read_text())
    assert saved['state'] == ('failed' if not mismatch and not live else 'running')
    assert not Path(cursor.context(config, 'finalize', '000')[2]['result']).exists()
    if saved['state'] == 'failed':
        assert json.loads((root / 'cursor_workers/000.reconcile.before.json').read_text()) == state


def test_cleanup_failed_holds_capacity_until_verified_reconciliation(tmp_path, monkeypatch):
    from bilibili_live_monitor import live_share_watch as watch
    config, plan, root = setup(tmp_path)
    plan['concurrency'] = 1
    atomic_json(root / 'plan.json', plan)
    calls = []
    monkeypatch.setattr(cursor, 'post', lambda *a: calls.append(a) or {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    state.update(state='cleanup_failed', adapter_pid=123, pgid=456)
    atomic_json(root / 'cursor_workers/000.json', state)
    with pytest.raises(ValueError, match='并发已满'):
        cursor.launch(config, 'finalize', '001')
    assert len(calls) == 1
    job = {'pid': 123, 'status': 'exited', 'exit_code': 1, 'exited_at': '2030-01-01',
           'command': [cursor.sys.executable, '-m', 'bilibili_live_monitor.cursor_finalize', 'worker',
                       '--config', config['_config_path'], '--kind', 'finalize',
                       '--shard', '000', '--token', state['token']]}
    monkeypatch.setattr(watch, 'request', lambda *a: job)
    monkeypatch.setattr(cursor, 'group_exists', lambda *a: False)
    assert cursor.reconcile_worker(config, 'finalize', '000')
    cursor.launch(config, 'finalize', '001')
    assert len(calls) == 2


@pytest.mark.parametrize('fault', ['interpreter', 'adapter_missing', 'adapter_wrong', 'config',
                                   'kind', 'shard', 'plan', 'nonterminal', 'no_exit', 'no_time', 'request'])
def test_reconcile_rejects_incomplete_or_changed_identity(tmp_path, monkeypatch, fault):
    from bilibili_live_monitor import live_share_watch as watch
    config, _, root = setup(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    state.update(state='running', adapter_pid=123, pgid=456)
    argv = [cursor.sys.executable, '-m', 'bilibili_live_monitor.cursor_finalize', 'worker',
            '--config', config['_config_path'], '--kind', 'finalize', '--shard', '000', '--token', state['token']]
    job = {'pid': 123, 'status': 'exited', 'exit_code': 1, 'exited_at': '2030', 'command': argv}
    if fault == 'interpreter':
        argv[0] = '/wrong/python'
    if fault == 'adapter_missing':
        state.pop('adapter_pid')
    if fault == 'adapter_wrong':
        state['adapter_pid'] = 999
    if fault in ('config', 'kind', 'shard'):
        argv[argv.index('--' + fault) + 1] = 'wrong'
    if fault == 'plan':
        state['plan_id'] = 'wrong'
    if fault == 'nonterminal':
        job['status'] = 'running'
    if fault == 'no_exit':
        job.pop('exit_code')
    if fault == 'no_time':
        job.pop('exited_at')
    path = root / 'cursor_workers/000.json'
    atomic_json(path, state)
    before = path.read_bytes()
    def request(*args):
        if fault == 'request':
            raise OSError('launcher unavailable')
        return job
    monkeypatch.setattr(watch, 'request', request)
    monkeypatch.setattr(cursor, 'group_exists', lambda *a: False)
    if fault == 'request':
        with pytest.raises(OSError):
            cursor.reconcile_worker(config, 'finalize', '000')
    elif fault == 'nonterminal':
        assert not cursor.reconcile_worker(config, 'finalize', '000')
    else:
        with pytest.raises(ValueError):
            cursor.reconcile_worker(config, 'finalize', '000')
    assert path.read_bytes() == before


def test_reconcile_cannot_touch_receipt_while_worker_holds_lock(tmp_path, monkeypatch):
    import fcntl
    config, _, root = setup(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    state.update(state='running', adapter_pid=123, pgid=456)
    path = root / 'cursor_workers/000.json'
    atomic_json(path, state)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            cursor.reconcile_worker(config, 'finalize', '000')
    assert json.loads(path.read_text()) == state


def test_cleanup_exception_persists_nonterminal_writer_state(tmp_path, monkeypatch):
    config, _, root = setup(tmp_path)
    monkeypatch.setattr(cursor, 'post', lambda *a: {'pid': 123, 'diagnose_on_failure': False})
    state = cursor.launch(config, 'finalize', '000')
    class Process:
        pid = 1234
        returncode = 0
        def __init__(self, *a, **kw):
            pass
        def wait(self, **kw):
            return 0
    monkeypatch.setattr(cursor.subprocess, 'Popen', Process)
    def denied(*a):
        raise PermissionError('live group not owned')
    monkeypatch.setattr(cursor, 'stop_group', denied)
    with pytest.raises(PermissionError):
        cursor.worker(config, 'finalize', '000', state['token'])
    receipt = json.loads((root / 'cursor_workers/000.json').read_text())
    assert receipt['state'] == 'cleanup_failed' and receipt['error'] == 'PermissionError'
