import json
import sqlite3

import pytest

from bilibili_live_monitor import session_timeline as module


@pytest.fixture
def setup(tmp_path, monkeypatch):
    database = tmp_path / 'monitor.sqlite3'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE untouched(text TEXT)')
        db.execute("INSERT INTO untouched VALUES('原始数据')")
    config = {'workspace': str(tmp_path), 'database': str(database), 'workflow_key': 'synthetic',
              'room_id': '123456', 'start_utc': '2030-01-01T00:00:00Z'}
    segment = {'seq': -1, 'text': '原始转写', 'corrected_text': '校正转写', 'is_final': True,
               'start_utc': '2030-01-01T00:00:10Z', 'end_utc': '2030-01-01T00:00:40Z',
               'correction_id': 'synthetic-correction', 'correction_reason': '弹幕给出拼写',
               'correction_evidence': [{'id': 'm:1', 'quote': '合成观众'}]}
    source = {'segments': [segment], 'items': [
        ('danmaku', '2030-01-01T00:00:05Z', {'author': '合成观众', 'text': '合成弹幕', '_record_id': 1}),
        ('gift', '2030-01-01T00:00:20Z', {'author': '合成送礼人', 'sender_uid': '42', '_record_id': 2,
                                        'gift': {'gift_name': '测试礼物', 'num': 3, 'total_coin': 6000}}),
        ('transcript', '2030-01-01T00:00:10Z', {'text': '旧在线稿'}),
        ('entry', '2030-01-01T00:00:30Z', {'author': '合成入场', '_record_id': 4}),
        ('like', '2030-01-01T00:00:35Z', {'author': '合成点赞', '_record_id': 5})]}
    monkeypatch.setattr(module, 'source_at_cutoff', lambda cfg: ({}, 1893456060, source))
    return config, source


def test_mixed_final_timeline_is_atomic_private_and_replayable(setup):
    config, _ = setup
    marker = module.commit(config, 1893456060)
    assert module.verify(config, 1893456060) == module.commit(config, 1893456060) == marker
    with sqlite3.connect(config['database']) as db:
        rows = [json.loads(raw) for raw, in db.execute('SELECT data FROM finalized_timeline ORDER BY position')]
        assert db.execute('SELECT text FROM untouched').fetchone()[0] == '原始数据'
    assert [r['kind'] for r in rows] == ['danmaku', 'transcript', 'gift', 'entry', 'like']
    assert rows[1]['text'] == '校正转写' and rows[1]['original_text'] == '原始转写'
    assert rows[1]['correction_evidence'] == [{'id': 'm:1', 'quote': '合成观众'}]
    assert rows[2]['data']['gift']['num'] == 3 and rows[2]['data']['author'] == '合成送礼人'
    from pathlib import Path
    assert not (Path(config['workspace']) / 'public').exists()


def test_wrong_identity_cutoff_and_database_damage_fail_verification(setup):
    config, _ = setup
    with pytest.raises(ValueError, match='截止点'):
        module.commit(config, 1893456070)
    module.commit(config, 1893456060)
    with pytest.raises(ValueError, match='身份'):
        module.verify({**config, 'room_id': '654321'}, 1893456060)
    with sqlite3.connect(config['database']) as db:
        db.execute('DELETE FROM finalized_timeline WHERE position=1')
    with pytest.raises(ValueError, match='完整性'):
        module.verify(config, 1893456060)
    with pytest.raises(ValueError, match='已变化'):
        module.commit(config, 1893456060)


def test_existing_final_version_is_not_silently_replaced(setup):
    config, source = setup
    module.commit(config, 1893456060)
    source['segments'][0]['corrected_text'] = '另一个版本'
    with pytest.raises(ValueError, match='不自动改写'):
        module.commit(config, 1893456060)


def test_uncommitted_talk_cannot_get_final_timeline(setup, monkeypatch):
    from bilibili_live_monitor import talk_finalize
    config, _ = setup
    monkeypatch.setattr(talk_finalize, 'complete', lambda c: False)
    with pytest.raises(ValueError, match='尚未完整入库'):
        module.commit({**config, 'talk_finalize': True}, 1893456060)
