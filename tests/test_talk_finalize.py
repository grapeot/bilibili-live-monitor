import json
import sqlite3

import pytest

from bilibili_live_monitor import talk_finalize as module


@pytest.fixture
def setup(tmp_path, monkeypatch):
    database = tmp_path / 'monitor.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE original(text TEXT)')
        db.execute("INSERT INTO original VALUES('原始ASR')")
    config = {'workspace': str(tmp_path), 'database': str(database), 'room_id': '123456',
              'workflow_key': 'synthetic', 'start_utc': '2030-01-01T00:00:00Z'}
    segments = [{'seq': i, 'start_utc': f'2030-01-01T00:{minute}:00Z',
                 'end_utc': f'2030-01-01T00:{minute}:30Z', 'text': '今天我们讨论语音识别。', 'is_final': True}
                for i, minute in [(1, '00'), (2, '31')]]
    monkeypatch.setattr(module, 'source_at_cutoff', lambda cfg: ({}, 1893459000, {'segments': segments}))
    return config, segments


def write_results(config):
    plan = module.prepare(config)
    for shard in plan['shards']:
        from pathlib import Path
        inputs = json.loads(Path(shard['input']).read_text())
        rows = [{**w, 'corrected_text': '今天，我们讨论语音识别。', 'confidence': 'high', 'reason': '补逗号'}
                for w in inputs['windows'] if w['seq'] in inputs['target_seqs']]
        Path(shard['result']).write_text(json.dumps({'plan_id': plan['id'], 'reviews': rows}))
    return plan


def test_atomic_versioned_commit_replay_and_overlay_keep_original(setup):
    config, segments = setup
    plan = write_results(config)
    assert len(plan['shards']) == 2
    marker = module.finish(config)
    assert module.complete(config)
    assert module.finish(config) == marker
    with sqlite3.connect(config['database']) as db:
        assert db.execute('SELECT COUNT(*) FROM transcript_revisions').fetchone()[0] == 2
        assert db.execute('SELECT text FROM original').fetchone()[0] == '原始ASR'
    result = module.apply_revisions(config, {'segments': segments})['segments']
    assert [s['text'] for s in result] == [s['text'] for s in segments]
    assert result[0]['corrected_text'] == '今天，我们讨论语音识别。'
    segments[0]['text'] = '已经变更'
    assert not module.complete(config)


@pytest.mark.parametrize('change', ['missing', 'fingerprint', 'rewrite', 'low', 'plan'])
def test_invalid_partial_or_stale_results_do_not_write_database(setup, change):
    from pathlib import Path
    config, _ = setup
    plan = write_results(config)
    path = Path(plan['shards'][0]['result'])
    result = json.loads(path.read_text())
    if change == 'missing':
        result['reviews'] = []
    elif change == 'plan':
        result['plan_id'] = 'wrong'
    else:
        result['reviews'][0][{'fingerprint': 'fingerprint', 'rewrite': 'corrected_text', 'low': 'confidence'}[change]] = {
            'fingerprint': 'wrong', 'rewrite': '完全虚构无关内容', 'low': 'low'}[change]
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError):
        module.finish(config)
    assert not module.complete(config)
    with sqlite3.connect(config['database']) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='transcript_revisions'").fetchall()


def test_database_tampering_invalidates_marker(setup):
    config, _ = setup
    write_results(config)
    module.finish(config)
    with sqlite3.connect(config['database']) as db:
        db.execute("DELETE FROM transcript_revisions WHERE seq=1")
    assert not module.complete(config)


def test_talk_prompt_prohibits_search_and_shared_writes(setup, monkeypatch):
    from bilibili_live_monitor import session_finalize
    config, _ = setup
    config['_config_path'] = '/synthetic/config.json'
    prompts = []
    monkeypatch.setattr(session_finalize, 'dispatch', lambda c, s, prompt, root, callback: prompts.append(prompt))
    module.submit(config, 'synthetic-session')
    assert '禁止联网搜索' in prompts[0] and '不能自己编写SQL' in prompts[0]
    assert '上下文窗口只读不输出' in prompts[0]


def test_danmaku_is_time_scoped_frozen_and_evidence_persisted(setup, monkeypatch):
    from pathlib import Path
    config, segments = setup
    source = {'segments': segments, 'items': [
        ('danmaku', '2030-01-01T00:00:05Z', {'_record_id': 1, 'author': '合成小鹿', 'text': '是语音识别呀'}),
        ('danmaku', '2030-01-01T00:31:05Z', {'_record_id': 2, 'author': '合成观众', 'text': '下一话题'}),
        ('danmaku', '2030-01-01T00:51:00Z', {'_record_id': 3, 'author': '下一场', 'text': '不得采用'}),
        ('gift', '2030-01-01T00:00:05Z', {'_record_id': 4, 'author': '送礼', 'text': '非弹幕'})]}
    monkeypatch.setattr(module, 'source_at_cutoff', lambda cfg: ({}, 1893459000, source))
    plan = write_results(config)
    first, second = [json.loads(Path(s['input']).read_text()) for s in plan['shards']]
    assert [m['id'] for m in first['danmaku']] == ['m:1']
    assert [m['id'] for m in second['danmaku']] == ['m:2']
    path = Path(plan['shards'][0]['result'])
    result = json.loads(path.read_text())
    result['reviews'][0]['evidence'] = [{'id': 'm:1', 'quote': '合成小鹿'}]
    path.write_text(json.dumps(result))
    module.finish(config)
    assert module.complete(config)
    assert module.database_rows(config, plan['id'])[0]['evidence'] == [{'id': 'm:1', 'quote': '合成小鹿'}]
    source['items'][0][2]['author'] = '已变化'
    assert not module.complete(config)


def test_invented_danmaku_evidence_is_rejected(setup):
    from pathlib import Path
    config, _ = setup
    plan = write_results(config)
    path = Path(plan['shards'][0]['result'])
    result = json.loads(path.read_text())
    result['reviews'][0]['evidence'] = [{'id': 'm:999', 'quote': '虚构'}]
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match='真实弹幕'):
        module.finish(config)
