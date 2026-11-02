import importlib.util
from pathlib import Path

from bilibili_live_monitor.live_share import fingerprint, make_snapshot

START = '2026-11-02T17:00:00+00:00'
NOW = '2026-11-02T17:05:00+00:00'


def snapshot(source, annotations=None):
    return make_snapshot(source, start=START, now=NOW, label='测试直播', phase='live',
                         health={'recording': True, 'secret': 'do not export'}, annotations=annotations,
                         approved_songs=('测试歌',))


def test_only_requested_metrics_are_exported_and_counts_are_distinct():
    result = snapshot({'raw': [(NOW, 'ONLINE_RANK_COUNT', {'data': {'online_count': 21, 'count': 50}}),
                               (NOW, 'WATCHED_CHANGE', {'data': {'num': 75}})],
                       'items': [('entry', NOW, {'sender_uid': 'private'}), ('gift', NOW, {'author': 'private'})],
                       'segments': []})
    assert result['latest']['online']['value'] == 21
    assert 'watched' not in result['latest'] and 'fan_club' not in result['series']
    assert result['counts']['entries'] == 1
    assert result['counts']['gifts'] == 1
    assert 'sender_uid' not in str(result) and 'secret' not in str(result)


def test_missing_is_not_zero_and_outside_session_is_ignored():
    result = snapshot({'raw': [('2026-11-01T10:00:00Z', 'WATCHED_CHANGE', {'data': {'num': 123}}),
                               (NOW, 'ONLINE_RANK_COUNT', {'data': {'online_count': None}})],
                       'items': [], 'segments': []})
    assert result['latest'] == {}
    assert result['series']['online'] == []


def test_annotation_requires_current_text_fingerprint_and_strips_extra_fields():
    segment = {'seq': 3, 'start_utc': START, 'end_utc': NOW, 'text': '私有原文'}
    review = {'seq': 3, 'fingerprint': fingerprint(segment), 'label': 'singing', 'song': '测试歌',
              'confidence': 'high', 'private': 'private'}
    source = {'raw': [], 'items': [], 'segments': [segment]}
    result = snapshot(source, {'reviews': [review]})
    assert result['activity'][0]['song'] == '测试歌'
    assert '私有原文' not in str(result) and 'private' not in str(result)
    segment['text'] = 'revised'
    assert snapshot(source, {'reviews': [review]})['activity'] == []


def test_terminal_requires_fresh_confirmed_offline_and_recording_stopped():
    path = Path(__file__).parents[1] / 'scripts/live_share_watch.py'
    spec = importlib.util.spec_from_file_location('watch_for_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = {'deadline': 9999999999, 'room_id': 'test', 'live_start': 'this-session'}
    platform = {'candidates': [{'room_id': 'test', 'status_checked_utc': NOW,
                               'live_status': 0, 'offline_checks': 2}],
                'sources': [{'room_id': 'test', 'status': 'stopped'}]}
    from bilibili_live_monitor.live_share import epoch
    assert module.terminal(config, platform, epoch(NOW))
    assert not module.terminal(config, platform, epoch(NOW) + 200)
    platform['sources'][0]['status'] = 'capturing'
    assert not module.terminal(config, platform, epoch(NOW))


def test_model_cannot_publish_private_song_or_timestamp_payload():
    segment = {'seq': 1, 'start_utc': START, 'end_utc': NOW, 'text': 'private nickname'}
    review = {'seq': 1, 'fingerprint': fingerprint(segment), 'label': 'singing',
              'song': 'private nickname', 'confidence': 'high'}
    result = snapshot({'raw': [], 'items': [], 'segments': [segment]},
                      {'reviews': [review], 'updated_at': {'private': 'nickname'}})
    assert result['activity'][0]['song'] is None
    assert result['ai_updated_at'] is None
    assert 'nickname' not in str(result)


def test_next_session_segment_end_is_clipped_out():
    segment = {'seq': 1, 'start_utc': NOW, 'end_utc': '2026-11-02T17:06:00Z', 'text': 'new broadcast'}
    result = snapshot({'raw': [], 'items': [], 'segments': [segment]}, {'reviews': [
        {'seq': 1, 'fingerprint': fingerprint(segment), 'label': 'singing', 'song': '测试歌'}]})
    assert result['activity'] == []


def test_missing_live_start_unknown_and_future_status_do_not_stop():
    path = Path(__file__).parents[1] / 'scripts/live_share_watch.py'
    spec = importlib.util.spec_from_file_location('watch_more_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from bilibili_live_monitor.live_share import epoch
    config = {'deadline': epoch(NOW) + 100, 'room_id': 'test', 'live_start': '2026-11-02 20:00:00',
              'start_utc': START}
    candidate = {'room_id': 'test', 'status_checked_utc': NOW, 'live_status': 1}
    platform = {'candidates': [candidate], 'sources': [{'room_id': 'test', 'status': 'stopped'}]}
    assert not module.terminal(config, platform, epoch(NOW))
    candidate.update(live_start='bad timestamp')
    assert not module.terminal(config, platform, epoch(NOW))
    candidate.update(live_status=2, offline_checks=2)
    assert module.terminal(config, platform, epoch(NOW))
    assert not module.terminal(config, platform, epoch(NOW) - 10)
    assert module.frozen_cutoff(config, {'last_live_at': epoch(START) + 30}, platform, epoch(NOW)) == epoch(START) + 30


def test_deadline_stops_even_when_api_unavailable(tmp_path, monkeypatch):
    import json
    path = Path(__file__).parents[1] / 'scripts/live_share_watch.py'
    spec = importlib.util.spec_from_file_location('watch_deadline_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'request', lambda *args: (_ for _ in ()).throw(OSError('offline')))
    config = {'deadline': 1, 'workspace': str(tmp_path), 'start_utc': START, 'api': 'http://unused'}
    state = module.cycle(config, tmp_path / 'state.json', 'run', upload=False)
    assert state['stop_intent'] and state['phase'] == 'completed'
    assert json.loads((tmp_path / 'state.json').read_text())['stop_intent']


def test_live_api_failure_is_persisted_and_does_not_mark_completion(tmp_path, monkeypatch):
    import json
    import time
    path = Path(__file__).parents[1] / 'scripts/live_share_watch.py'
    spec = importlib.util.spec_from_file_location('watch_failure_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'request', lambda *args: (_ for _ in ()).throw(OSError('temporary outage')))
    config = {'deadline': time.time() + 500, 'workspace': str(tmp_path), 'api': 'http://unused'}
    state = module.cycle(config, tmp_path / 'state.json', 'run', upload=False)
    assert state['phase'] == 'live' and not state.get('stop_intent')
    assert 'temporary outage' in json.loads((tmp_path / 'state.json').read_text())['last_error']


def test_stop_intent_never_invokes_ai(monkeypatch):
    path = Path(__file__).parents[1] / 'scripts/live_share_watch.py'
    spec = importlib.util.spec_from_file_location('watch_stop_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('called')))
    module.ai_once({}, {'stop_intent': True}, {}, 100)


def test_cycle_old_live_confirmation_never_includes_later_events(tmp_path, monkeypatch):
    import json

    from bilibili_live_monitor.live_share import epoch
    path = Path(__file__).parents[1] / 'scripts/live_share_watch.py'
    spec = importlib.util.spec_from_file_location('watch_cross_session_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    now = epoch(NOW)
    confirmed = now - 100
    candidate = {'room_id': 'test', 'status_checked_utc': '2026-11-02T17:03:20Z',
                 'live_status': 1, 'live_start': '2026-11-03 01:00:00'}
    platform = {'candidates': [candidate], 'sources': [], 'message_health': {}}
    api_state = {'rooms': [{'room_id': 'test', 'recorded_seconds': 100, 'recording_bytes': 100,
                           'recording_status': 'recording', 'asr_enabled': True}],
                 'asr_ready': True, 'queue_depth': 0}
    monkeypatch.setattr(module.time, 'time', lambda: now)
    monkeypatch.setattr(module, 'request', lambda url: platform if url.endswith('/platform') else api_state)
    monkeypatch.setattr(module, 'read_source', lambda *args: {
        'raw': [('2026-11-02T17:04:30Z', 'WATCHED_CHANGE', {'data': {'num': 999}})],
        'items': [('entry', '2026-11-02T17:04:30Z', {})], 'segments': []})
    config = {'workspace': str(tmp_path), 'database': 'unused', 'room_id': 'test', 'start_utc': START,
              'live_start': candidate['live_start'], 'label': 'Test', 'api': 'http://unused',
              'deadline': now + 1000}
    state = module.cycle(config, tmp_path / 'state.json', 'once', upload=False)
    assert state['last_live_at'] == confirmed
    snapshot_data = json.loads((tmp_path / 'public/snapshot.json').read_text())
    assert snapshot_data['latest'] == {} and snapshot_data['counts']['entries'] == 0
    candidate['live_start'] = '2026-11-03 01:04:00'
    final = module.cycle(config, tmp_path / 'state.json', 'once', upload=False)
    assert final['cutoff'] == confirmed and final['stop_intent']
    assert json.loads((tmp_path / 'public/snapshot.json').read_text())['counts']['entries'] == 0


def test_ai_input_has_same_start_end_bounds(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from bilibili_live_monitor.live_share import epoch
    path = Path(__file__).parents[1] / 'scripts/live_share_watch.py'
    spec = importlib.util.spec_from_file_location('watch_ai_scope_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = SimpleNamespace(stdout='{}', stderr='', check_returncode=lambda: None)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: result)
    config = {'workspace': str(tmp_path), 'start_utc': START, 'deadline': epoch(NOW) + 100,
              'opencode_python': 'unused', 'opencode_cwd': '.', 'ai_session': 'test', 'model': 'test'}
    source = {'raw': [], 'items': [], 'segments': [
        {'seq': 1, 'start_utc': START, 'end_utc': NOW, 'text': 'allowed', 'is_final': True},
        {'seq': 2, 'start_utc': START, 'end_utc': '2026-11-02T17:06:00Z', 'text': 'cross', 'is_final': True},
        {'seq': 3, 'start_utc': '2026-11-02T17:06:00Z', 'end_utc': NOW, 'text': 'inverted', 'is_final': True},
        {'seq': 4, 'start_utc': '2026-11-01T17:00:00Z', 'end_utc': NOW, 'text': 'old', 'is_final': True}]}
    module.ai_once(config, {'last_live_at': epoch(NOW)}, source, epoch(NOW))
    assert [w['seq'] for w in json.loads((tmp_path / 'ai_input.json').read_text())['windows']] == [1]


def gift_item(uid='123', ident='one', gift_id='31164', blind=None):
    return ('gift', START, {'author': '合成观众', 'sender_uid': uid, 'identity_status': 'uid',
                            'gift': {'event_type': 'SEND_GIFT_V2', 'gift_id': gift_id,
                                     'gift_name': '粉丝团灯牌', 'sender_uid': uid, 'num': 1,
                                     'event_id': ident, 'blind_gift': blind}})


def test_lamp_pending_is_programmatic_deduplicated_and_accepts_both_ids():
    from bilibili_live_monitor.live_share import followup_candidates
    source = {'items': [gift_item(), gift_item(), gift_item(ident='two', gift_id='34001')],
              'raw': [], 'segments': []}
    cards = followup_candidates(source, START)
    assert len(cards) == 1 and cards[0]['gift_count'] == 2
    result = snapshot(source, {'completed_followups': [{'id': 'invented-by-model', 'evidence': ['fake']}]})
    assert len(result['followups']) == 1 and result['followups'][0]['status'] == 'pending'
    assert '123' not in str(result['followups'])


def test_blind_box_creates_followup_but_badge_and_unrelated_gift_do_not():
    from bilibili_live_monitor.live_share import followup_candidates
    source = {'items': [('danmaku', START, {'sender_uid': '123', 'medal': {'level': 10}}),
                        gift_item(blind={'original_gift_id': 1}), gift_item(ident='bad', gift_id='wrong')],
              'raw': [], 'segments': []}
    cards = followup_candidates(source, START)
    assert len(cards) == 1
    assert cards[0]['blind_orders'] == 1 and cards[0]['lamp_count'] == 0


def test_completion_needs_existing_card_and_valid_post_gift_evidence():
    from bilibili_live_monitor.live_share import followup_candidates, followup_evidence
    source = {'items': [gift_item(), ('danmaku', NOW, {'_record_id': 1, 'sender_uid': 'other', 'text': 'test'})],
              'raw': [], 'segments': []}
    card = followup_candidates(source, START)[0]
    annotations = {'completed_followups': [{'id': card['id'], 'evidence': ['m:1']}]}
    assert snapshot(source, annotations)['followups'][0]['status'] == 'pending'
    source['segments'] = [{'seq': 1, 'start_utc': START, 'end_utc': NOW,
                           'text': 'already fulfilled', 'is_final': True}]
    ref = followup_evidence(source)[0]['id']
    annotations['completed_followups'][0]['evidence'] = [ref]
    result = snapshot(source, annotations)['followups'][0]
    assert result['status'] == 'done' and result['name'] == '合成观众'
    annotations['completed_followups'][0]['evidence'] = ['unavailable']
    assert snapshot(source, annotations)['followups'][0]['status'] == 'pending'


def test_live_renderer_reuses_four_charts_singing_and_gaps():
    from bilibili_live_monitor.live_share import render_snapshot
    segment = {'seq': 3, 'start_utc': '2026-11-02T17:01:00Z',
               'end_utc': '2026-11-02T17:04:00Z', 'text': '私有歌词'}
    review = {'seq': 3, 'fingerprint': fingerprint(segment), 'label': 'singing',
              'music_source': 'probable_live', 'song': '测试歌', 'confidence': 'high'}
    source = {'raw': [(NOW, 'ONLINE_RANK_COUNT', {'data': {'online_count': 21}})],
              'items': [gift_item()], 'segments': [segment]}
    result = snapshot(source, {'reviews': [review]})
    page = render_snapshot(result)
    assert page.count('data-chart=') == 4
    assert page.index('data-chart="entries"') < page.index('data-chart="online"') < page.index('data-chart="fans"') < page.index('data-chart="gifts"')
    assert 'data-song-title="测试歌"' in page and 'data-missing-audio="true"' in page
    assert '私有歌词' not in page and '粉丝团人数' not in page and '人看过' not in page
    assert 'song-tooltip' in page and 'DOMParser' in page
    review['music_source'] = 'recording'
    assert 'data-song-title="测试歌"' not in render_snapshot(snapshot(source, {'reviews': [review]}))


def test_ai_keeps_early_followup_evidence_and_backfills_legacy_labels(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from bilibili_live_monitor.live_share import epoch
    spec = importlib.util.spec_from_file_location('watch_history', Path(__file__).parents[1] / 'scripts/live_share_watch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = SimpleNamespace(stdout='{}', stderr='', check_returncode=lambda: None)
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: result)
    segment = {'seq': 1, 'start_utc': START, 'end_utc': NOW, 'text': 'early confirmation', 'is_final': True}
    (tmp_path / 'annotations.json').write_text(json.dumps({'reviews': [
        {'seq': 1, 'fingerprint': fingerprint(segment), 'label': 'singing'}]}))
    source = {'raw': [], 'segments': [segment], 'items': [gift_item()] + [
        ('danmaku', NOW, {'_record_id': n, 'sender_uid': '123', 'text': 'test'}) for n in range(401)]}
    from bilibili_live_monitor.live_share import followup_candidates
    previous = json.loads((tmp_path / 'annotations.json').read_text())
    previous['song_requests'] = [{'id': followup_candidates(source, START)[0]['id'],
                                  'song': '请求中的测试歌', 'stage': 'accepted', 'evidence': ['m:0']}]
    (tmp_path / 'annotations.json').write_text(json.dumps(previous))
    config = {'workspace': str(tmp_path), 'start_utc': START, 'deadline': epoch(NOW) + 100,
              'opencode_python': 'unused', 'opencode_cwd': '.', 'ai_session': 'test', 'model': 'test'}
    module.ai_once(config, {'last_live_at': epoch(NOW)}, source, epoch(NOW))
    data = json.loads((tmp_path / 'ai_input.json').read_text())
    assert any(e['kind'] == 'transcript' for e in data['followup_evidence'])
    assert 'm:0' in [e['id'] for e in data['followup_evidence']]
    assert data['windows'][0]['seq'] == 1
    assert data['unpublished_songs'] == [{'song': '请求中的测试歌', 'seqs': []}]
    full = json.loads((tmp_path / 'full_context.json').read_text())
    assert len(full['evidence']) == 402


def test_blind_result_duplicates_and_new_gift_reopens_followup():
    from bilibili_live_monitor.live_share import followup_candidates, followup_evidence
    first = gift_item(blind={'original_gift_id': 32251})
    source = {'raw': [], 'items': [first, first], 'segments': [
        {'seq': 1, 'start_utc': START, 'end_utc': '2026-11-02T17:02:00Z',
         'text': '接到点歌', 'is_final': True}]}
    card = followup_candidates(source, START)[0]
    assert card['blind_orders'] == 1
    annotations = {'completed_followups': [{'id': card['id'], 'evidence': [followup_evidence(source)[0]['id']]}]}
    assert snapshot(source, annotations)['followups'][0]['status'] == 'done'
    second = gift_item(ident='two', blind={'original_gift_id': 32251})
    source['items'].append((second[0], NOW, second[2]))
    result = snapshot(source, annotations)['followups'][0]
    assert result['blind_orders'] == 2 and result['status'] == 'pending'


def test_requested_song_requires_allowed_title_and_linked_evidence():
    from bilibili_live_monitor.live_share import followup_candidates, followup_evidence
    source = {'raw': [], 'items': [gift_item()], 'segments': [
        {'seq': 1, 'start_utc': START, 'end_utc': NOW, 'text': '已接单', 'is_final': True}]}
    request = {'id': followup_candidates(source, START)[0]['id'], 'song': '测试歌', 'stage': 'accepted',
               'evidence': [followup_evidence(source)[0]['id']]}
    assert snapshot(source, {'song_requests': [request]})['followups'][0]['request_song'] == '测试歌'
    request['song'] = 'private information'
    assert 'request_song' not in snapshot(source, {'song_requests': [request]})['followups'][0]
    request.update(song='测试歌', evidence=['m:missing'])
    assert 'request_song' not in snapshot(source, {'song_requests': [request]})['followups'][0]


def test_verified_song_registry_checks_public_page_and_local_path(tmp_path):
    import json

    from bilibili_live_monitor.song_sources import verified_titles
    root = tmp_path / 'song_sources'
    root.mkdir()
    (root / 'source.json').write_text(json.dumps({'data': {'results': [
        {'title': '测试歌 歌词', 'url': 'https://example.org/song', 'raw_content': '歌词正文'}]}}))
    entries = [{'title': '测试歌', 'file': 'source.json', 'result_index': 0},
               {'title': 'private nickname', 'file': 'source.json', 'result_index': 0},
               {'title': '测试歌', 'file': '../outside.json', 'result_index': 0}]
    (tmp_path / 'verified_songs.json').write_text(json.dumps(entries))
    assert verified_titles(tmp_path) == ('测试歌',)
    (root / 'source.json').write_text(json.dumps({'data': {'results': [None]}}))
    assert verified_titles(tmp_path) == ()
    (root / 'source.json').write_text(json.dumps({'data': {'results': [
        {'title': '测试歌', 'url': 7, 'raw_content': '歌词'}]}}))
    assert verified_titles(tmp_path) == ()


def test_song_request_respects_newer_human_awaiting_override():
    from bilibili_live_monitor.live_share import followup_candidates, followup_evidence, public_followups
    source = {'raw': [], 'items': [gift_item()], 'segments': [
        {'seq': 1, 'start_utc': START, 'end_utc': NOW, 'text': '已唱', 'is_final': True}]}
    request = {'id': followup_candidates(source, START)[0]['id'], 'song': '测试歌', 'stage': 'performed',
               'evidence': [followup_evidence(source)[0]['id']]}
    result = public_followups(source, START, {'song_requests': [request]},
                              {'123': {'status': 'awaiting_response', 'at': NOW}}, ('测试歌',))
    assert result[0]['status'] == 'awaiting_response' and 'request_song' not in result[0]


def test_lamp_batch_items_distinct_but_blind_results_one_order():
    from bilibili_live_monitor.live_share import followup_candidates
    items = []
    for index in range(3):
        item = gift_item()
        item[2]['gift']['batch_index'] = index
        items.append(item)
    source = {'raw': [], 'items': items + items, 'segments': []}
    assert followup_candidates(source, START)[0]['lamp_count'] == 3
    for item in items:
        item[2]['gift']['blind_gift'] = {'original_gift_id': 32251}
    card = followup_candidates(source, START)[0]
    assert card['lamp_count'] == 0 and card['blind_orders'] == 1


def test_stop_api_failure_rebuilds_visible_html(tmp_path, monkeypatch):
    import json

    from bilibili_live_monitor.live_share import atomic_json, render_snapshot
    spec = importlib.util.spec_from_file_location('watch_final_page', Path(__file__).parents[1] / 'scripts/live_share_watch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'request', lambda *a: (_ for _ in ()).throw(OSError('offline')))
    public = tmp_path / 'public'
    public.mkdir()
    data = snapshot({'raw': [], 'items': [], 'segments': []})
    atomic_json(public / 'snapshot.json', data)
    (public / 'index.html').write_text(render_snapshot(data))
    config = {'deadline': 1, 'workspace': str(tmp_path), 'start_utc': START, 'api': 'http://unused'}
    module.cycle(config, tmp_path / 'state.json', 'run', upload=False)
    assert '本场已结束' in (public / 'index.html').read_text()
    assert '直播中' not in (public / 'index.html').read_text()
    assert json.loads((public / 'snapshot.json').read_text())['phase'] == 'completed'


def test_captains_and_user_overrides_take_precedence():
    from bilibili_live_monitor.live_share import followup_candidates, followup_evidence, public_followups
    source = {'raw': [], 'items': [gift_item()], 'segments': [
        {'seq': 1, 'start_utc': START, 'end_utc': NOW, 'text': 'confirmed', 'is_final': True}]}
    card = followup_candidates(source, START)[0]
    refs = [followup_evidence(source)[0]['id']]
    annotations = {'completed_followups': [{'id': card['id'], 'evidence': refs}]}
    assert public_followups(source, START, annotations, {'123': {'status': 'excluded'}}) == []
    assert public_followups(source, START, {}, {'123': {'status': 'done'}})[0]['status'] == 'done'
    override = {'123': {'status': 'awaiting_response', 'at': NOW}}
    assert public_followups(source, START, annotations, override)[0]['status'] == 'awaiting_response'
    override['123']['at'] = START
    assert public_followups(source, START, annotations, override)[0]['status'] == 'done'
    source['items'].append(('danmaku', NOW, {'sender_uid': '123', 'guard_level': 3}))
    assert followup_candidates(source, START) == []
