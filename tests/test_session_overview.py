from bilibili_live_monitor.session_overview import session_overview

PAGE = '<html><title>Anonymous</title><main data-revision="1"><h1>Anonymous</h1><p>chart</p></main></html>'


def test_visible_metrics_and_metadata_preserve_refresh_main():
    page = session_overview(PAGE, 'Test room', {
        'phase': 'live', 'session_summary': {'watched': 1234},
        'latest': {'online': {'value': 0, 'at': '2026-01-01T12:00:00+00:00'}}})
    assert '<title>Test room · 单场直播分析</title>' in page
    assert '<h1>Test room · 单场直播分析</h1>' in page
    assert 'og:title' in page and 'name="description"' in page
    assert '<strong>1,234</strong>' in page and '<strong>0</strong>' in page
    assert '01/01 20:00:00' in page and '最近上报在线人数' in page
    assert page.index('session-overview"') > page.index('<main')
    assert '<main data-revision="1">' in page and '<p>chart</p>' in page


def test_missing_counts_and_stopped_online_are_explicit():
    page = session_overview(PAGE, 'Room', {})
    assert page.count('<strong>—</strong>') == 2
    assert '尚未收到平台累计观看数据' in page
    page = session_overview(PAGE, 'Room', {'phase': 'finalizing', 'latest': {'online': {'value': 18}}})
    assert '下播前在线人数' in page and '下播前最后值' in page


def test_room_name_is_escaped_in_html_and_regex_replacement():
    page = session_overview(PAGE, r'<script>"\1&', {})
    assert '<script>' not in page
    assert '&lt;script&gt;' in page
    assert '\\1' in page
