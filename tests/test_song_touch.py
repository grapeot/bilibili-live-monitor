"""Real touch regression; optional locally when Chromium is installed."""
from pathlib import Path

import pytest

from bilibili_live_monitor.trends_render import render


def test_touch_and_keyboard_song_tooltip():
    playwright = pytest.importorskip('playwright.sync_api')
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Chromium is not installed')
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width':390,'height':844},
                                      is_mobile=True, has_touch=True)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        data = {'title':'合成测试','timezone':'UTC','since':'2026-01-01T00:00:00+00:00',
                'until':'2026-01-01T00:10:00+00:00',
                'summary':{'gaps':[], 'unique_entry_uids':0,'gift_events':0}}
        start = 1767225600
        songs = [{'id':'song-0','title':'合成歌名','segments':[{'start':start,'end':start+600}]}]
        charts = [{'field':'gifts','name':'礼物','color':'#2563eb','mode':'bar','note':'',
                   'points':[{'t':start,'end':start+600,'v':1,'partial':False,'label':'合成数据'}]}]
        page.set_content(render(data, songs=songs, charts=charts, show_song_list=False))
        bar = page.locator('svg rect[fill="#2563eb"]')
        bar.scroll_into_view_if_needed()
        box = bar.bounding_box()
        page.touchscreen.tap(box['x']+min(120,box['width']/2),box['y']+30)
        tip = page.locator('#song-tooltip')
        assert tip.is_visible()
        assert tip.locator('strong').inner_text() == '合成歌名'
        bounds = tip.bounding_box()
        assert 0 <= bounds['x'] and bounds['x']+bounds['width'] <= 390
        page.locator('h1').click()
        assert tip.is_hidden()
        band = page.locator('.song-hit')
        band.focus()
        page.keyboard.press('Enter')
        assert tip.is_visible()
        page.keyboard.press('Escape')
        assert tip.is_hidden()
        assert not errors
        context.close()
        browser.close()
