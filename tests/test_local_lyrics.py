import json
from unittest.mock import patch

import pytest

from bilibili_live_monitor.local_lyrics import agent_prompt, export_library, library_root
from bilibili_live_monitor.song_sources import register_source, verified_titles
from bilibili_live_monitor.transcript_corrections import correction_context


def catalog(tmp_path):
    path = tmp_path / 'catalog.json'
    songs = [
        {'work_id': 'zh', 'title': '示例歌', 'artist': '示例歌手', 'language': 'zh', 'status': 'ready',
         'lines': ['爱与梦', '爱与梦'], 'sources': [{'url': 'https://example.com/song'}]},
        {'work_id': 'ja', 'title': '愛してる', 'language': 'ja', 'status': 'partial',
         'lines': ['世界が終わるまで'], 'sources': [{'url': 'https://example.com/japanese'}]},
        {'work_id': 'missing', 'title': '无词', 'status': 'missing', 'lines': [], 'sources': []}]
    path.write_text(json.dumps({'songs': songs}, ensure_ascii=False))
    return path


def test_export_keeps_text_and_reuses_existing_source_and_correction_contract(tmp_path):
    root = tmp_path / 'library'
    result = export_library(catalog(tmp_path), root)
    assert result['stats'] == {'ready': 1, 'partial': 1, 'missing': 1, 'pending': 0}
    assert (root / 'songs/zh.txt').read_text() == '爱与梦\n爱与梦\n'
    assert (root / 'songs/ja.txt').read_text() == '世界が終わるまで\n'
    index = [json.loads(line) for line in (root / 'index.jsonl').read_text().splitlines()]
    assert index[-1]['source'] is None
    register_source(tmp_path, '示例歌', root / 'sources/zh.json', 0)
    register_source(tmp_path, '示例歌', root / 'sources/zh.json', 0)
    assert verified_titles(tmp_path) == ('示例歌',)
    assert len(json.loads((tmp_path / 'verified_songs.json').read_text())) == 1
    _, context = correction_context(tmp_path, {'items': [], 'segments': []})
    assert len(context['sources']) == 1 and context['sources'][0]['id'].startswith('s:')
    assert '本地整理歌词快照' in (root / 'sources/zh.json').read_text()
    with pytest.raises(FileExistsError):
        export_library(catalog(tmp_path), root)


def test_agent_uses_files_not_preselected_lyrics_and_children_cannot_register(tmp_path):
    root = tmp_path / 'library'
    export_library(catalog(tmp_path), root)
    config = {'lyric_library': str(root), '_config_path': str(tmp_path / 'config.json')}
    realtime = agent_prompt(config)
    final = agent_prompt(config, final=True)
    assert str(root / 'index.jsonl') in realtime
    assert '爱与梦' not in realtime
    assert '优先可信' in realtime and '不再例行联网' in realtime
    assert '确认与本场实际唱词匹配' in realtime
    assert '子任务只在自己的result' in final
    assert '总协调统一执行登记' in final
    assert agent_prompt({'lyric_library': str(tmp_path / 'absent')}) == ''
    with pytest.raises(ValueError):
        library_root({'lyric_library': 'relative'})
    with patch('bilibili_live_monitor.local_lyrics.Path.home', return_value=tmp_path):
        assert library_root({}) == tmp_path / '.local/share/bilibili-live-monitor/lyrics/current'


def test_rejects_duplicate_and_traversing_ids_before_creating_output(tmp_path):
    path = catalog(tmp_path)
    data = json.loads(path.read_text())
    data['songs'][0]['work_id'] = '../escape'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        export_library(path, tmp_path / 'bad')
    assert not (tmp_path / 'bad').exists()


def test_existing_final_coordinator_receives_library_without_changing_plan(tmp_path, monkeypatch):
    from test_session_finalize import fixture

    from bilibili_live_monitor import session_finalize

    root = tmp_path / 'library'
    export_library(catalog(tmp_path), root)
    config, _ = fixture(tmp_path)
    config['lyric_library'] = str(root)
    plan = session_finalize.prepare(config)
    captured = []
    monkeypatch.setattr(session_finalize, 'dispatch', lambda c, s, prompt, r, cb: captured.append(prompt))
    session_finalize.submit(config, 'existing-session')
    assert str(root / 'index.jsonl') in captured[0]
    assert '子任务只在自己的result' in captured[0]
    assert session_finalize.prepare(config) == plan
