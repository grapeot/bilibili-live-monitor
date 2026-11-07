import json

import pytest

from bilibili_live_monitor.audience_navigation import with_audience_links
from bilibili_live_monitor.live_share import make_snapshot, render_snapshot


@pytest.mark.parametrize('phase', ['live', 'completed'])
def test_real_session_renderer_supports_audience_navigation(tmp_path, phase):
    """实际趋势模板省略可选body标签，直播及最终页均应继续生成。"""
    snapshot = make_snapshot({'raw': [], 'items': [], 'segments': []},
        start='2026-01-01T10:00:00+00:00', now='2026-01-01T10:05:00+00:00',
        label='测试直播', phase=phase, health={})
    assert '</body>' not in render_snapshot(snapshot)
    navigation = tmp_path / 'navigation.json'
    navigation.write_text(json.dumps({'home_url': 'https://example.com/',
                                      'audience_url': 'https://example.com/audience/'}))
    page = render_snapshot(snapshot, navigation=navigation)
    assert page.count('id="audience-navigation"') == 1
    assert page.index('id="audience-navigation"') < page.rindex('</html>')
    updated = with_audience_links(page, 'https://example.com/new-audience/')
    assert updated.count('id="audience-navigation"') == 1
    assert 'https://example.com/new-audience/' in updated
    assert 'https://example.com/audience/' not in updated
    assert with_audience_links(updated, 'https://example.com/new-audience/') == updated


def test_navigation_rejects_document_without_any_closing_boundary():
    with pytest.raises(ValueError):
        with_audience_links('<main>incomplete', 'https://example.com/audience/')
