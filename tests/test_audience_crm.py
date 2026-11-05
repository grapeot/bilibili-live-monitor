import copy
import json

import pytest

from bilibili_live_monitor.audience_crm import collect, finish, person_id, prepare, validate_state
from bilibili_live_monitor.audience_pages import build, with_audience_links


def config(tmp_path, room='10001'):
    return {'room_id': room, 'scope': 'example', 'secret': 'x' * 48, 'label': '示例主播',
            'workspace': str(tmp_path / 'private'), 'output_dir': str(tmp_path / 'public'),
            'home_url': 'https://example.com/s/random-token/'}


def session(room='10001'):
    return {'id': 'one', 'room_id': room, 'start': '2026-01-01T00:00:00+00:00',
            'end': '2026-01-01T01:00:00+00:00', 'label': '一月一日',
            'url': 'https://example.com/s/another-token/',
            'source': {'raw': [], 'segments': [{'seq': 1, 'text': '好，下次学这首歌。', 'is_final': True,
                'start_utc': '2026-01-01T00:01:00+00:00', 'end_utc': '2026-01-01T00:01:10+00:00'}],
                'items': [('danmaku', '2026-01-01T00:00:50+00:00',
                           {'sender_uid': '101', 'author': '同名', 'text': '想听示例歌', 'source_key': 'a'}),
                          ('entry', '2026-01-01T00:02:00+00:00',
                           {'sender_uid': '102', 'author': '同名', 'source_key': 'b'})]}}


def state_for(person):
    own = next(e for e in person['evidence'] if e['speaker'] == 'viewer')
    return {'person_id': person['id'], 'revision': person['revision'],
            'overview': [{'text': '曾点示例歌', 'refs': [{'id': own['id'], 'quote': '想听示例歌'}]}],
            'preferences': [], 'attention': [], 'promises': [], 'actions': [], 'interactions': []}


def test_identity_scope_and_names_not_identity(tmp_path):
    cfg = config(tmp_path)
    material = collect(cfg, [session()])
    assert len(material['people']) == 2
    assert person_id(cfg['secret'], '10001', '101') != person_id(cfg['secret'], '10002', '101')
    assert all('duration' not in p for p in material['people'].values())
    with pytest.raises(ValueError):
        collect(cfg, [session('10002')])
    with pytest.raises(ValueError):
        collect(cfg, [session(), session()])


def test_gift_dedup_totals_and_unknowns(tmp_path):
    one = session()
    gift = {'sender_uid': '101', 'author': '新昵称', 'gift': {'sender_uid': '101',
            'event_id': 'g1', 'event_type': 'SEND_GIFT_V2', 'num': 3, 'coin_type': 'gold', 'total_coin': 9000}}
    one['source']['items'] += [('gift', '2026-01-01T00:03:00+00:00', gift)] * 2
    one['source']['items'] += [('gift', '2026-01-01T00:04:00+00:00',
                               {**gift, 'gift': {**gift['gift'], 'sender_uid': '999'}})]
    p = next(p for p in collect(config(tmp_path), [one])['people'].values() if p['uid'] == '101')
    assert p['counts']['gift_events'] == 1
    assert p['counts']['gift_count'] == 3
    assert p['counts']['amount_milli'] == 9000
    assert p['names'] == ['同名', '新昵称']


def test_refs_cannot_cross_people_or_invent_promises(tmp_path):
    p = next(p for p in collect(config(tmp_path), [session()])['people'].values() if p['evidence'])
    state = state_for(p)
    assert validate_state(p, state) == state
    bad = copy.deepcopy(state)
    bad['overview'][0]['refs'][0]['quote'] = '不存在的原话'
    with pytest.raises(ValueError):
        validate_state(p, bad)
    bad = copy.deepcopy(state)
    bad['promises'] = [{**state['overview'][0], 'status': 'open'}]
    with pytest.raises(ValueError):
        validate_state(p, bad)


def test_complete_versions_and_public_whitelist(tmp_path):
    cfg = config(tmp_path)
    material = collect(cfg, [session()])
    directory, plan = prepare(cfg, material)
    assert len(plan['tasks']) == 1
    with pytest.raises(FileNotFoundError):
        finish(cfg, directory)
    assert not (tmp_path / 'private/current.json').exists()
    for task in plan['tasks']:
        from pathlib import Path
        Path(task['result']).write_text(json.dumps(state_for(material['people'][task['person_id']]), ensure_ascii=False))
    assert finish(cfg, directory)['people'] == 2
    assert finish(cfg, directory)['status'] == 'completed'
    assert prepare(cfg, material) == (directory, plan)
    result = build(cfg)
    assert result['people'] == 2
    index = json.loads((tmp_path / 'public/index.json').read_text())
    assert 'uid' not in json.dumps(index)
    for path in (tmp_path / 'public').rglob('*.html'):
        text = path.read_text()
        assert 'sender_uid' not in text and str(tmp_path) not in text
    p = next(p for p in material['people'].values() if p['evidence'])
    assert '想听示例歌' in (tmp_path / 'public' / p['id'] / 'index.html').read_text()


def test_stale_completion_cannot_roll_back(tmp_path):
    cfg = config(tmp_path)
    first = collect(cfg, [session()])
    one, _ = prepare(cfg, first)
    other = session()
    other['source']['items'][0][2]['text'] = '改听另一首歌'
    second = collect(cfg, [other])
    two, plan = prepare(cfg, second)
    from pathlib import Path
    for directory, material in ((one, first), (two, second)):
        tasks = json.loads((directory / 'plan.json').read_text())['tasks']
        for task in tasks:
            state = state_for(material['people'][task['person_id']])
            state['overview'] = []
            Path(task['result']).write_text(json.dumps(state))
    finish(cfg, two)
    with pytest.raises(ValueError, match='不能回退'):
        finish(cfg, one)
    assert json.loads((tmp_path / 'private/current.json').read_text())['plan_id'] == plan['id']


def test_navigation_is_idempotent_and_safe():
    text = '<html><body><main></main></body></html>'
    url = 'https://example.com/s/secret/audience/'
    updated = with_audience_links(text, url)
    assert with_audience_links(updated, url) == updated
    assert updated.count('id="audience-navigation"') == 1
    with pytest.raises(ValueError):
        with_audience_links(text, 'javascript:alert(1)')


def test_later_named_fulfillment_without_viewer_events_is_recalled(tmp_path):
    cfg = config(tmp_path)
    before = collect(cfg, [session()])
    later = session()
    later.update(id='two', start='2026-01-02T00:00:00+00:00', end='2026-01-02T01:00:00+00:00')
    later['source'] = {'raw': [], 'items': [], 'segments': [{'seq': 2, 'is_final': True,
        'start_utc': '2026-01-02T00:01:00+00:00', 'end_utc': '2026-01-02T00:01:10+00:00',
        'text': '同名，昨天答应的示例歌已经学会，现在给你补唱。'}]}
    after = collect(cfg, [session(), later])
    pid = person_id(cfg['secret'], cfg['room_id'], '101')
    assert before['people'][pid]['revision'] != after['people'][pid]['revision']
    assert any(e['session_id'] == 'two' for e in after['people'][pid]['evidence'])
    assert len(after['people'][pid]['sessions']) == 1  # 提名不是到访证明


def test_public_projection_hides_contacts_ids_and_paths(tmp_path):
    from bilibili_live_monitor.audience_pages import detail
    one = session()
    raw = '想听示例歌，邮箱 foo@example.net，UID:101，本地 /Users/example/private.txt，加我微信：wx_demo_77，账号是101'
    one['source']['items'][0][2]['text'] = raw
    cfg = config(tmp_path)
    material = collect(cfg, [one])
    p = next(p for p in material['people'].values() if p['evidence'])
    state = state_for(p)
    state['overview'][0]['refs'][0]['quote'] = raw
    state['overview'][0]['text'] += ' foo@example.net UID:101 /Users/example/private.txt'
    validate_state(p, state)  # 私有证据仍然逐字，公开另作最小投影
    text = detail(p, state, material, cfg)
    assert 'foo@example.net' not in text and 'UID:101' not in text and '/Users/example' not in text
    assert 'wx_demo_77' not in text and '账号是101' not in text
    assert '想听示例歌' in text


def test_navigation_script_closing_tag_is_escaped():
    from html.parser import HTMLParser

    class Scripts(HTMLParser):
        count = 0

        def handle_starttag(self, tag, attrs):
            self.count += tag == 'script'

    parser = Scripts()
    parser.feed(with_audience_links('<html><body></body></html>', 'https://example.com/</script><script>alert(1)</script>'))
    assert parser.count == 1


def test_failed_build_preserves_entire_previous_site(tmp_path, monkeypatch):
    from pathlib import Path
    cfg = config(tmp_path)
    material = collect(cfg, [session()])
    directory, plan = prepare(cfg, material)
    for task in plan['tasks']:
        Path(task['result']).write_text(json.dumps(state_for(material['people'][task['person_id']])))
    finish(cfg, directory)
    build(cfg)
    output = Path(cfg['output_dir'])
    before = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()}
    original = Path.write_text
    counter = 0

    def fail_second(path, value, *args, **kwargs):
        nonlocal counter
        if path.name == 'index.html':
            counter += 1
            if counter == 2:
                raise OSError('second page failure')
        return original(path, value, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', fail_second)
    with pytest.raises(OSError, match='second page'):
        build(cfg)
    after = {str(p.relative_to(output)): p.read_bytes() for p in output.rglob('*') if p.is_file()}
    assert before == after


@pytest.mark.parametrize('author_agent', [None, 'grok'])
def test_finalize_dispatch_once_then_builds_only_accepted_state(tmp_path, monkeypatch, author_agent):
    from pathlib import Path

    from bilibili_live_monitor import audience_finalize, session_ai, session_finalize, session_runner
    cfg = config(tmp_path)
    if author_agent:
        cfg['author_agent'] = author_agent
    config_path = tmp_path / 'settings.json'
    config_path.write_text(json.dumps(cfg))
    scene = tmp_path / 'scene'
    scene.mkdir()
    conf = {'room_id': cfg['room_id'], 'workspace': str(scene), 'audience_crm_config': str(config_path),
            '_config_path': str(scene / 'config.json')}
    material = collect(cfg, [session()])
    monkeypatch.setattr('bilibili_live_monitor.audience_crm.load_material', lambda _: material)
    monkeypatch.setattr(session_runner, 'ensure_ai', lambda *_: 'synthetic-session')
    monkeypatch.setattr(session_runner, 'stop_ai', lambda *_, **__: True)
    monkeypatch.setattr(session_ai, 'ai_control', lambda *_, **__: True)
    calls = []

    def dispatch(*args):
        calls.append(args)
        args[-1]()

    monkeypatch.setattr(session_finalize, 'dispatch', dispatch)
    state = {}
    assert not audience_finalize.advance(conf, state)
    assert not audience_finalize.advance(conf, state)
    assert len(calls) == 1
    assert f"使用task的{author_agent or 'ollama_glm_5_3_flash'}子代理" in calls[0][2]
    saved = json.loads((scene / 'audience_finalize/plan.json').read_text())
    directory = Path(saved['run'])
    plan = json.loads((directory / 'plan.json').read_text())
    for task in plan['tasks']:
        Path(task['result']).write_text(json.dumps(state_for(material['people'][task['person_id']])))
    finish(cfg, directory)
    assert audience_finalize.advance(conf, state)
    assert state['audience_crm']['people'] == 2


@pytest.mark.parametrize('agentic, override, expected', [
    (True, None, 'grok'), (False, None, 'ollama_glm_5_3_flash'),
    (True, 'custom-reviewer', 'custom-reviewer'),
])
def test_finalize_author_route(tmp_path, agentic, override, expected):
    from bilibili_live_monitor.audience_finalize import load_settings
    path = tmp_path / 'settings.json'
    settings = {'room_id': '123456', 'agentic_sources': agentic}
    if override is not None:
        settings['author_agent'] = override
    path.write_text(json.dumps(settings))
    loaded, _ = load_settings({'room_id': '123456', 'audience_crm_config': str(path)})
    assert loaded['author_agent'] == expected


def test_symlink_workspace_can_finish(tmp_path):
    from pathlib import Path
    target = tmp_path / 'real'
    target.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(target, target_is_directory=True)
    cfg = {**config(tmp_path), 'workspace': str(alias / 'private')}
    material = collect(cfg, [session()])
    directory, plan = prepare(cfg, material)
    for task in plan['tasks']:
        Path(task['result']).write_text(json.dumps(state_for(material['people'][task['person_id']])))
    assert finish(cfg, directory)['status'] == 'completed'


def test_timeout_stops_audience_session_tree(tmp_path, monkeypatch):
    from bilibili_live_monitor import session_runner
    from bilibili_live_monitor.live_share import atomic_json
    cfg = {'workspace': str(tmp_path), 'ai_session': 'live', 'talk_finalize': False, 'finalize_parallel': False}
    atomic_json(tmp_path / 'workflow.json', {'stage': 'finalizing', 'finish_deadline': -1,
                                          'final_session': 'final', 'audience_session': 'audience'})
    monkeypatch.setattr(session_runner, 'load_config', lambda _: cfg)
    monkeypatch.setattr('bilibili_live_monitor.session_health.ensure_scheduled', lambda *_, **__: {})
    stopped = []
    monkeypatch.setattr(session_runner, 'stop_ai', lambda _, ident, **kw: stopped.append((ident, kw['children'])) or True)
    assert session_runner.run(tmp_path / 'config.json') == 1
    assert ('audience', True) in stopped
