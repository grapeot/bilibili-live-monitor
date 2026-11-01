"""页面按ASCII宽1、非ASCII的UTF-16单元宽2计数。"""
from bilibili_live_monitor.welcome import text_length, welcome_messages


def test_combined_or_split_always_respects_limit_with_emoji_names():
    for limit in (80, 100, 120):
        result = welcome_messages(['😀合成观众甲', '合成观众乙', '合成观众丙', '合成观众丁'], limit)
        assert 1 <= len(result) <= 2
        assert all(text_length(text) <= limit for text in result)
