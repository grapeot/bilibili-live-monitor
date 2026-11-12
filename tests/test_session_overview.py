from bilibili_live_monitor.session_overview import session_overview

PAGE = ('<html><title>Anonymous</title><main data-revision="1"><h1>Anonymous</h1>'
        '<nav class="archive-nav"><a href="https://example.org/home/">返回主页</a>'
        '<a href="https://example.org/songs/">歌曲搜索</a></nav><p>旧场次时间</p>'
        '<section data-chart="online"><h2>在线人数</h2></section><footer>'
        '<section id="session-summary"><h2>本场汇总</h2><table>旧汇总</table></section></footer></main></html>')
SNAPSHOT = {'phase': 'live', 'session_start': '2026-01-01T12:00:00+00:00',
            'updated_at': '2026-01-01T13:00:00+00:00',
            'session_summary': {'watched': 1234, 'followers_change': -2, 'gift_count': 0,
                                'gift_amount_yuan': 12.5, 'gift_amount_unknown': True}}


def test_one_navigation_then_title_time_cards_and_charts():
    page = session_overview(PAGE, 'Test room', SNAPSHOT,
                            navigation={'home_url': '/instances/example/rooms/123456/',
                                        'audience_url': 'https://example.org/audience/'})
    assert '<title>Test room · 单场直播分析</title>' in page
    assert 'og:title' in page and 'name="description"' in page
    assert '<main data-revision="1">' in page
    assert page.index('session-header"') < page.index('<h1>') < page.index('class="session-meta"')
    assert page.index('class="session-meta"') < page.index('id="session-summary"') < page.index('data-chart="online"')
    assert page.count('id="session-summary"') == 1 and '旧汇总' not in page and '旧场次时间' not in page
    assert page.count('<nav ') == 1 and page.count('id="audience-entry"') == 1
    assert 'href="/instances/example/rooms/123456/"' in page
    assert 'href="https://example.org/home/"' not in page
    for text in ['1,234', '-2', '>0</strong>', '¥12.50+', '2026-01-01 20:00:00', '直播中']:
        assert text in page
    assert 'session-overview"' not in page
    assert session_overview(page, 'Test room', SNAPSHOT) == page


def test_missing_counts_negative_growth_and_unknown_values():
    page = session_overview(PAGE, 'Room', {})
    assert page.count('<strong>—</strong>') == 4
    assert '旧场次时间' in page
    page = session_overview(PAGE, 'Room', {'session_summary': {'followers_change': 3,
                            'gift_count': 0, 'gift_count_unknown': True}})
    assert '<strong>+3</strong>' in page and '<strong>0+</strong>' in page


def test_room_name_and_navigation_are_escaped_and_unsafe_targets_ignored():
    page = session_overview(PAGE, r'<script>"\1&', {},
                            navigation={'home_url': 'javascript:alert(1)', 'songs_url': '//evil.example',
                                        'audience_url': 'https://example.org/?a="&b=1'})
    assert '<script>' not in page and '&lt;script&gt;' in page and '\\1' in page
    assert 'javascript:' not in page and '//evil.example' not in page
    assert 'href="https://example.org/?a=&quot;&amp;b=1"' in page


def test_cross_day_completed_and_attention_metadata():
    snapshot = {**SNAPSHOT, 'phase': 'completed', 'updated_at': '2026-01-02T01:00:00+00:00'}
    page = session_overview(PAGE, 'Room', snapshot)
    assert '2026-01-02 09:00:00' in page and '本场已结束' in page and '直播中' not in page
    page = session_overview(PAGE, 'Room', {**snapshot, 'publication_status': 'attention'})
    assert '分析待恢复' in page
