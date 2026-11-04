"""主播内容类型决定是否执行内容AI，不改变原始ASR。"""


def content_ai_enabled(config):
    return config.get('content_mode', 'music') == 'music'


def apply_content_mode(snapshot, config):
    snapshot['content_mode'] = config.get('content_mode', 'music')
    if not content_ai_enabled(config):
        # 不把未做语义判定的ASR冒充已复核杂谈片段。
        snapshot.update(activity=[], activity_summary=[], followups=[])
    return snapshot
