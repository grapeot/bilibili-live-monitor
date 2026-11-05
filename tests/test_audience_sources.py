import copy
import json
from pathlib import Path

import pytest

from bilibili_live_monitor.audience_crm import collect, finish, prepare, validate_state
from bilibili_live_monitor.audience_pages import build


def test_agent_reads_full_session_and_cites_delayed_response(tmp_path):
    cfg = {'room_id': '10001', 'scope': 'example', 'secret': 'x' * 48, 'label': '示例',
           'workspace': str(tmp_path / 'private'), 'output_dir': str(tmp_path / 'public'),
           'home_url': 'https://example.com/s/home/', 'agentic_sources': True}
    session = {'id': 'one', 'room_id': '10001', 'start': '2026-01-01T00:00:00+00:00',
        'end': '2026-01-01T01:00:00+00:00', 'label': '示例场', 'url': 'https://example.com/s/one/',
        'documents': [{'kind': 'song_activity', 'name': 'songs', 'content': {'song': '示例歌'}}],
        'source': {'raw': [], 'items': [
            ('danmaku', '2026-01-01T00:01:00+00:00', {'sender_uid': '101', 'author': '观众一', 'text': '点歌 示例歌'}),
            ('danmaku', '2026-01-01T00:02:00+00:00', {'sender_uid': '102', 'author': '观众二', 'text': '我也在'})],
            'segments': [{'seq': 1, 'is_final': True, 'text': '唱完了，示例歌送给观众一。',
                          'start_utc': '2026-01-01T00:31:00+00:00', 'end_utc': '2026-01-01T00:31:30+00:00'}]}}
    material = collect(cfg, [session])
    directory, plan = prepare(cfg, material)
    person = next(p for p in material['people'].values() if p['uid'] == '101')
    task = next(t for t in plan['tasks'] if t['person_id'] == person['id'])
    payload = json.loads(Path(task['input']).read_text())
    assert all(e['speaker'] == 'viewer' for e in payload['evidence'])
    manifest = json.loads(Path(payload['source_manifest']).read_text())
    rows = [json.loads(line) for line in Path(manifest['sessions'][0]['transcripts']['path']).read_text().splitlines()]
    assert len(rows) == 1
    own = person['evidence'][0]
    state = {'person_id': person['id'], 'revision': person['revision'], 'overview': [], 'preferences': [],
             'attention': [], 'actions': [], 'interactions': [], 'promises': [
                 {'text': '示例歌已回应', 'status': 'fulfilled', 'refs': [
                     {'id': own['id'], 'quote': '点歌 示例歌'},
                     {'id': rows[0]['id'], 'quote': '唱完了，示例歌送给观众一。'}]}]}
    assert validate_state(person, state, material) == state
    other = next(p for p in material['people'].values() if p['uid'] == '102')
    invalid = copy.deepcopy(state)
    invalid['promises'][0]['refs'][0] = {'id': other['evidence'][0]['id'], 'quote': '我也在'}
    with pytest.raises(ValueError, match='本人'):
        validate_state(person, invalid, material)
    for t in plan['tasks']:
        p = material['people'][t['person_id']]
        value = state if p['id'] == person['id'] else {**state, 'person_id': p['id'], 'revision': p['revision'], 'promises': []}
        Path(t['result']).write_text(json.dumps(value))
    finish(cfg, directory)
    build(cfg)
    text = (Path(cfg['output_dir']) / person['id'] / 'index.html').read_text()
    assert '唱完了，示例歌送给观众一。' in text
    assert 'source_manifest' not in text and str(tmp_path) not in text
    transcript = Path(manifest['sessions'][0]['transcripts']['path'])
    transcript.write_text('tampered')
    with pytest.raises(ValueError, match='冻结整场证据'):
        finish(cfg, directory)
    transcript.unlink()
    with pytest.raises(ValueError, match='缺失'):
        finish(cfg, directory)
    assert not transcript.exists()


def test_later_session_changes_revision_even_without_viewer_return(tmp_path, monkeypatch):
    cfg = {'room_id': '10001', 'scope': 'example', 'secret': 'x' * 48,
           'label': '示例', 'workspace': str(tmp_path), 'agentic_sources': True}
    first = {'id': 'one', 'room_id': '10001', 'start': '2026-01-01T00:00:00+00:00',
             'end': '2026-01-01T01:00:00+00:00', 'label': '一', 'url': 'https://example.com/one/',
             'source': {'raw': [], 'segments': [], 'items': [('danmaku', '2026-01-01T00:01:00+00:00',
                        {'sender_uid': '101', 'author': '观众一', 'text': '想听示例歌'})]}}
    before = collect(cfg, [first])
    second = {**first, 'id': 'two', 'start': '2026-01-02T00:00:00+00:00', 'end': '2026-01-02T01:00:00+00:00',
              'source': {'raw': [], 'items': [], 'segments': [{'seq': 2, 'is_final': True,
                  'text': '昨天的歌准备好了。', 'start_utc': '2026-01-02T00:31:00+00:00',
                  'end_utc': '2026-01-02T00:31:30+00:00'}]}}
    after = collect(cfg, [first, second])
    pid = next(iter(before['people']))
    assert before['people'][pid]['revision'] != after['people'][pid]['revision']
    assert len(after['people'][pid]['sessions']) == 1
    assert any(e['session_id'] == 'two' for e in after['source_evidence'].values())
    from bilibili_live_monitor import audience_crm
    monkeypatch.setattr(audience_crm, 'AGENTIC_PROMPT', audience_crm.AGENTIC_PROMPT + '\n新查证合同')
    revised = collect(cfg, [first])
    assert revised['analysis_contract'] != before['analysis_contract']
    assert revised['people'][pid]['revision'] != before['people'][pid]['revision']


@pytest.mark.parametrize('layer', ['original', 'online', 'legacy'])
def test_foreign_room_transcripts_are_rejected(tmp_path, monkeypatch, layer):
    from bilibili_live_monitor.audience_crm import load_material
    cfg = {'room_id': '10001', 'scope': 'example', 'secret': 'x' * 48,
           'label': '示例', 'workspace': str(tmp_path), 'agentic_sources': True,
           'database': str(tmp_path / 'db.sqlite')}
    segment = {'room_id': '99999', 'seq': 1, 'is_final': True, 'text': '唱完了',
               'start_utc': '2026-01-01T00:01:00+00:00', 'end_utc': '2026-01-01T00:01:30+00:00'}
    scene = {'id': 'one', 'room_id': '10001', 'start': '2026-01-01T00:00:00+00:00',
             'end': '2026-01-01T01:00:00+00:00', 'label': '一', 'url': 'https://example.com/one/',
             'source': {'raw': [], 'segments': [], 'items': []}}
    if layer == 'original':
        scene['source']['segments'] = [segment]
    elif layer == 'online':
        scene['online_evidence'] = [{'id': 't:1', 'room_id': '99999', 'kind': 'transcript',
                                     'at': segment['start_utc'], 'text': '唱完了'}]
    else:
        path = tmp_path / 'legacy.jsonl'
        path.write_text(json.dumps(segment) + '\n')
        cfg['legacy_sessions'] = [{k: v for k, v in scene.items() if k != 'source'} |
                                  {'offline_transcript': str(path)}]
        monkeypatch.setattr('bilibili_live_monitor.live_share.read_source', lambda *_: scene['source'])
        with pytest.raises(ValueError, match='其他房间'):
            load_material(cfg)
        return
    with pytest.raises(ValueError, match='其他房间'):
        collect(cfg, [scene])


def test_external_context_must_match_session_identity(tmp_path):
    from bilibili_live_monitor.audience_sources import attach_session_files
    (tmp_path / 'config.json').write_text(json.dumps({'room_id': '99999'}))
    with pytest.raises(ValueError, match='其他房间'):
        attach_session_files({'room_id': '10001', 'start': '2026-01-01T00:00:00+00:00'}, tmp_path)
