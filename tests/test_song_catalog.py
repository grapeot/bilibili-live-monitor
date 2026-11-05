import json

import pytest

from bilibili_live_monitor.song_catalog import build, catalog, performances, render, safe_url
from bilibili_live_monitor.song_navigation import with_song_catalog_links, with_song_navigation


def config(tmp_path, scope='anchor-a', activity=None):
    snapshot = tmp_path / (scope + '.json')
    snapshot.write_text(json.dumps({'phase': 'completed', 'session_start': '2030-01-01T00:00:00Z',
                                    'activity': activity or [window(0, 30), window(38, 60)]}))
    return {'scope': scope, 'label': '合成主播', 'home_url': 'https://example.test/archive/',
            'output_dir': str(tmp_path / scope / 'public'), 'state_file': str(tmp_path / scope / 'state.json'),
            'sessions': [{'id': 's1', 'label': '合成场次', 'url': '/session/one/', 'snapshot': str(snapshot)}]}


def window(start, end, title='测试歌', source='probable_live', label='singing'):
    from bilibili_live_monitor.live_share import iso
    base = 1893456000
    return {'start': iso(base + start), 'end': iso(base + end), 'label': label,
            'music_source': source, 'song': title}


def test_same_song_short_gap_one_performance_with_original_spans(tmp_path):
    data = catalog(config(tmp_path))
    assert len(data['songs']) == 1 and data['songs'][0]['count'] == 1
    assert len(data['songs'][0]['performances'][0]['segments']) == 2


def test_repeats_and_other_sessions_are_preserved(tmp_path):
    conf = config(tmp_path, activity=[window(0, 30), window(30, 80, label='speaking'), window(80, 110)])
    conf['sessions'].append({**conf['sessions'][0], 'id': 's2', 'url': '/session/two/'})
    song = catalog(conf)['songs'][0]
    assert song['count'] == 4 and len({p['id'] for p in song['performances']}) == 4


def test_cards_prioritize_performance_count_over_recency(tmp_path):
    conf = config(tmp_path, activity=[window(0, 20, '常唱'), window(20, 80, label='speaking'), window(80, 100, '常唱'),
                                      window(200, 220, '最近单次'), window(150, 170, '较早单次')])
    data = catalog(conf)
    assert [(s['title'], s['count']) for s in data['songs']] == [('常唱', 2), ('最近单次', 1), ('较早单次', 1)]
    page = render(data)['index.html']
    assert page.index('<h2>常唱</h2>') < page.index('<h2>最近单次</h2>')


def test_catalog_links_are_optional_safe_and_idempotent():
    page = '<html><body><table id="segments"></table></body></html>'
    assert with_song_catalog_links(page) == page
    result = with_song_catalog_links(page, 'https://example.test/songs/')
    assert with_song_catalog_links(result, 'https://example.test/songs/') == result
    assert result.count('id="song-catalog-links"') == 1
    with pytest.raises(ValueError, match='HTTPS'):
        with_song_catalog_links(page, 'javascript:alert(1)')


def test_recordings_unknowns_and_unfinished_sessions_not_indexed(tmp_path):
    conf = config(tmp_path, activity=[window(0, 30, source='recording'), window(40, 60, title=None)])
    assert not catalog(conf)['songs']
    path = conf['sessions'][0]['snapshot']
    data = json.loads(open(path).read())
    data['phase'] = 'live'
    data['activity'] = [window(0, 30)]
    from pathlib import Path
    Path(path).write_text(json.dumps(data))
    assert not catalog(conf)['songs']


def test_anchor_identity_separate_and_cross_state_rejected(tmp_path):
    a, b = config(tmp_path), config(tmp_path, scope='anchor-b')
    assert catalog(a)['songs'][0]['id'] != catalog(b)['songs'][0]['id']
    with pytest.raises(ValueError):
        catalog(a, {'scope': 'anchor-b', 'performances': []})


def test_id_survives_boundary_correction(tmp_path):
    conf = config(tmp_path)
    first = catalog(conf)['songs'][0]['performances'][0]
    previous = {'scope': conf['scope'], 'performances': [first]}
    from pathlib import Path
    Path(conf['sessions'][0]['snapshot']).write_text(json.dumps({'phase': 'completed', 'activity': [window(2, 58)]}))
    second = catalog(conf, previous)['songs'][0]['performances'][0]
    assert second['id'] == first['id'] and second['start'] != first['start']


def test_merged_old_performances_keep_alias_anchors(tmp_path):
    conf = config(tmp_path, activity=[window(0, 30), window(30, 60, label='speaking'), window(60, 90)])
    old = catalog(conf)['songs'][0]['performances']
    from pathlib import Path
    Path(conf['sessions'][0]['snapshot']).write_text(json.dumps({'phase': 'completed', 'activity': [window(0, 90)]}))
    data = catalog(conf, {'scope': conf['scope'], 'performances': old})
    row = data['songs'][0]['performances'][0]
    assert {row['id'], *row['aliases']} == {p['id'] for p in old}


def test_html_is_escaped_and_output_excludes_raw_input(tmp_path):
    conf = config(tmp_path, activity=[window(0, 30, title='<img onerror=alert(1)>')])
    data = catalog(conf)
    pages = render(data)
    assert '<img onerror' not in ''.join(pages.values())
    result = build(conf)
    assert result['songs'] == 1
    from pathlib import Path
    public = (Path(conf['output_dir']) / 'index.json').read_text()
    assert 'snapshot' not in public and 'raw' not in public and str(tmp_path) not in public


def test_legacy_html_deduplicates_four_charts(tmp_path):
    path = tmp_path / 'old.html'
    path.write_text('<rect class="song-hit" data-song-title="测试歌" data-start="100" data-end="130"></rect>' * 4)
    result = performances({'html': str(path)})
    assert len(result) == 1 and result[0]['end'] - result[0]['start'] == 30


def test_legacy_dynamic_detail_data_is_included(tmp_path):
    path = tmp_path / 'dynamic.html'
    path.write_text('<script id="detail-data" type="application/json">' + json.dumps({
        'songs': [{'title': '测试歌', 'start': 100, 'end': 130}]}) + '</script>')
    rows = performances({'html': str(path)})
    assert len(rows) == 1 and rows[0]['title'] == '测试歌'


def test_split_merge_split_keeps_original_ids_unique(tmp_path):
    from pathlib import Path
    conf = config(tmp_path, activity=[window(0, 20), window(20, 40, label='speaking'), window(40, 60)])
    first = catalog(conf)['songs'][0]['performances']
    Path(conf['sessions'][0]['snapshot']).write_text(json.dumps({'phase': 'completed', 'activity': [window(0, 60)]}))
    merged = catalog(conf, {'scope': conf['scope'], 'performances': first})['songs'][0]['performances']
    Path(conf['sessions'][0]['snapshot']).write_text(json.dumps({'phase': 'completed', 'activity': [window(0, 20), window(20, 40, label='speaking'), window(40, 60)]}))
    last = catalog(conf, {'scope': conf['scope'], 'performances': merged})['songs'][0]['performances']
    assert {p['start']: p['id'] for p in first} == {p['start']: p['id'] for p in last}
    assert len({p['id'] for p in last}) == 2


def test_split_assigns_old_identity_to_best_overlapping_new_record(tmp_path):
    from pathlib import Path
    conf = config(tmp_path, activity=[window(0, 40), window(40, 60, label='speaking'), window(60, 100)])
    first = catalog(conf)['songs'][0]['performances']
    Path(conf['sessions'][0]['snapshot']).write_text(json.dumps({'phase': 'completed', 'activity': [window(0, 100)]}))
    merged = catalog(conf, {'scope': conf['scope'], 'performances': first})['songs'][0]['performances']
    Path(conf['sessions'][0]['snapshot']).write_text(json.dumps({'phase': 'completed', 'activity': [window(0, 65), window(65, 81, label='speaking'), window(81, 110)]}))
    final = catalog(conf, {'scope': conf['scope'], 'performances': merged})['songs'][0]['performances']
    assert final[0]['id'] == first[0]['id']
    assert final[1]['id'] == first[1]['id']


def test_identity_follows_successive_overlapping_boundary_corrections(tmp_path):
    from pathlib import Path
    conf = config(tmp_path, activity=[window(0, 20)])
    records = catalog(conf)['songs'][0]['performances']
    original_id = records[0]['id']
    for start, end in [(15, 35), (30, 50)]:
        Path(conf['sessions'][0]['snapshot']).write_text(json.dumps({'phase': 'completed', 'activity': [window(start, end)]}))
        records = catalog(conf, {'scope': conf['scope'], 'performances': records})['songs'][0]['performances']
        assert records[0]['id'] == original_id


def test_navigation_is_idempotent_and_urls_are_scoped():
    html = with_song_navigation('<body></body>')
    assert with_song_navigation(html) == html
    assert 'song-navigation' in with_song_navigation('<html><main>旧版</main></html>')
    for url in ['javascript:alert(1)', 'https://other.test/foo', '//other.test/']:
        with pytest.raises(ValueError):
            safe_url(url, 'https://example.test/archive/')


def test_duplicate_session_rejected(tmp_path):
    conf = config(tmp_path)
    conf['sessions'].append(conf['sessions'][0])
    with pytest.raises(ValueError):
        catalog(conf)
