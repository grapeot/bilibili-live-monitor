from bilibili_live_monitor.chart_titles import right_title, with_chart_titles


def test_right_title_is_inside_viewbox_and_escaped():
    text = right_title('每分钟入场 <test>')
    assert 'x="1098"' in text and 'text-anchor="end"' in text
    assert 'y="-10"' in text
    assert '&lt;test&gt;' in text and '<test>' not in text


def test_historical_injection_is_idempotent_and_supports_implicit_body():
    page = '<html><main><svg viewBox="0 0 1000 250" aria-label="在线人数"></svg></main></html>'
    result = with_chart_titles(page)
    assert with_chart_titles(result) == result
    assert result.endswith('</main></html>')
    assert 'MutationObserver' in result


def test_shared_template_labels_all_four_charts_on_right():
    from bilibili_live_monitor.live_share import make_snapshot, render_snapshot
    snapshot = make_snapshot({'raw': [], 'items': [], 'segments': []}, start='2030-01-01T00:00:00Z',
                             now='2030-01-01T00:10:00Z', label='合成直播', phase='completed', health={})
    page = render_snapshot(snapshot)
    assert page.count('<text data-chart-title-side="right"') == 4
    assert page.count('viewBox="0 -26 1110 278"') == 4
    for title in ('每分钟入场事件', '在线人数', '关注人数', '每分钟礼物事件'):
        assert title in page
    assert page.count('id="right-chart-titles"') == 1
