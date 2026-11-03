import copy
import json
from types import SimpleNamespace

import pytest

from bilibili_live_monitor import live_share_watch as watch
from bilibili_live_monitor.live_share import atomic_json, epoch, fingerprint, make_snapshot
from bilibili_live_monitor.transcript_corrections import correction_context, review_window

START = '2026-01-01T00:00:00Z'
END = '2026-01-01T00:00:30Z'


def fixture(tmp_path):
    segment = {'seq': 1, 'start_utc': START, 'end_utc': END, 'text': '接下来唱测试哥', 'is_final': True}
    source = {'raw': [], 'items': [('danmaku', END, {'_record_id': 9, 'text': '点歌：测试歌'})],
              'segments': [segment]}
    row = {'seq': 1, 'fingerprint': fingerprint(segment), 'corrected_text': '接下来唱测试歌',
           'reason': '与本场点歌拼写对应', 'evidence': [{'id': 'm:9', 'quote': '点歌：测试歌'}]}
    atomic_json(tmp_path / 'transcript_corrections.json', {'corrections': [row]})
    return source, row


def test_verified_overlay_keeps_original_and_fingerprint(tmp_path):
    source, _ = fixture(tmp_path)
    original = copy.deepcopy(source)
    active, info = correction_context(tmp_path, source)
    window = review_window(source['segments'][0], active)
    assert window['text'] == '接下来唱测试哥'
    assert window['corrected_text'] == '接下来唱测试歌'
    assert window['fingerprint'] == fingerprint(source['segments'][0])
    assert source == original and info['rejected'] == []
    assert make_snapshot(source, start=START, now=END, label='测试', phase='live',
                         health={}, annotations={}) == make_snapshot(original, start=START,
                         now=END, label='测试', phase='live', health={}, annotations={})


@pytest.mark.parametrize('change', ['fingerprint', 'missing', 'quote', 'bad_id', 'empty', 'no_reason'])
def test_invalid_or_stale_correction_not_applied(tmp_path, change):
    source, row = fixture(tmp_path)
    if change == 'fingerprint':
        source['segments'][0]['text'] = '离线回填得到的新原稿'
    elif change == 'missing':
        row['evidence'][0]['id'] = 'm:404'
    elif change == 'quote':
        row['evidence'][0]['quote'] = '并不存在的内容'
    elif change == 'bad_id':
        row['evidence'][0]['id'] = {}
    elif change == 'empty':
        row['corrected_text'] = ''
    else:
        row['reason'] = ''
    atomic_json(tmp_path / 'transcript_corrections.json', {'corrections': [row]})
    active, info = correction_context(tmp_path, source)
    assert active == {} and len(info['rejected']) == 1


def test_lyrics_evidence_requires_local_registered_source_and_exact_quote(tmp_path):
    source, row = fixture(tmp_path)
    root = tmp_path / 'song_sources'
    root.mkdir()
    atomic_json(root / 'lyric.json', {'data': {'results': [
        {'title': '测试歌 歌词', 'url': 'https://example.org/song', 'raw_content': '作词：示例作者\n测试歌词'}]}})
    atomic_json(tmp_path / 'verified_songs.json', [
        {'title': '测试歌', 'file': 'lyric.json', 'result_index': 0},
        {'title': '测试歌', 'file': '../outside.json', 'result_index': 0}])
    row['evidence'] = [{'id': 's:lyric.json:0', 'quote': '作词：示例作者'}]
    atomic_json(tmp_path / 'transcript_corrections.json', {'corrections': [row]})
    active, info = correction_context(tmp_path, source)
    assert active[1]['corrected_text'] == row['corrected_text']
    assert len(info['sources']) == 1 and info['rejected'] == []
    atomic_json(tmp_path / 'verified_songs.json', [])
    assert correction_context(tmp_path, source)[0] == {}


def test_bad_optional_document_does_not_break_review(tmp_path):
    source, _ = fixture(tmp_path)
    (tmp_path / 'transcript_corrections.json').write_text('{')
    assert correction_context(tmp_path, source)[0] == {}


def test_registered_title_is_evidence_even_when_not_repeated_in_body(tmp_path):
    source, row = fixture(tmp_path)
    root = tmp_path / 'song_sources'
    root.mkdir()
    atomic_json(root / 'title.json', {'data': {'results': [
        {'title': '测试歌 歌词', 'url': 'https://example.org/song', 'raw_content': '第一句歌词'}]}})
    atomic_json(tmp_path / 'verified_songs.json', [
        {'title': '测试歌', 'file': 'title.json', 'result_index': 0}])
    row['evidence'] = [{'id': 's:title.json:0', 'quote': '测试歌'}]
    atomic_json(tmp_path / 'transcript_corrections.json', {'corrections': [row]})
    assert correction_context(tmp_path, source)[0][1]['corrected_text'] == '接下来唱测试歌'


def test_malformed_optional_source_path_does_not_break_review(tmp_path):
    source, _ = fixture(tmp_path)
    atomic_json(tmp_path / 'verified_songs.json', [
        {'title': '测试歌', 'file': 'bad\0.json', 'result_index': 0}])
    active, info = correction_context(tmp_path, source)
    assert active[1]['corrected_text'] == '接下来唱测试歌'
    assert info['sources'] == []


def test_new_correction_requeues_old_window_and_reaches_next_ai_input(tmp_path, monkeypatch):
    source, _ = fixture(tmp_path)
    # The corrected old window is outside the rolling forty-window tail.
    source['segments'] += [dict(source['segments'][0], seq=i) for i in range(2, 45)]
    reviews = [{'seq': s['seq'], 'fingerprint': fingerprint(s), 'label': 'speaking',
                'music_source': 'none', 'investigation_version': 2} for s in source['segments']]
    atomic_json(tmp_path / 'annotations.json', {'reviews': reviews})
    monkeypatch.setattr('bilibili_live_monitor.session_ai.ai_control', lambda *a, **k: {'type': 'idle'})
    monkeypatch.setattr(watch.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(stdout='{}', stderr='', check_returncode=lambda: None))
    config = {'workspace': str(tmp_path), 'start_utc': START, 'deadline': epoch(END) + 100,
              'opencode_python': 'unused', 'opencode_cwd': '.', 'ai_session': 'test', 'model': 'test'}
    watch.ai_once(config, {'last_live_at': epoch(END)}, source, epoch(END))
    data = json.loads((tmp_path / 'ai_input.json').read_text())
    old = next(w for w in data['windows'] if w['seq'] == 1)
    assert old['corrected_text'] == '接下来唱测试歌'
    assert old['text'] == source['segments'][0]['text']
    assert 'transcript_corrections.json' in (tmp_path / 'ai_prompt.md').read_text()
    reviews[0]['correction_id'] = old['correction_id']
    atomic_json(tmp_path / 'annotations.json', {'reviews': reviews})
    watch.ai_once(config, {'last_live_at': epoch(END)}, source, epoch(END))
    assert 1 not in [w['seq'] for w in json.loads((tmp_path / 'ai_input.json').read_text())['windows']]
