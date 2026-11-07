"""分析与页面共用的带时区时间转换，不依赖渲染或采集。"""
from datetime import datetime, timezone


def epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('时间必须带时区')
    return parsed.timestamp()


def iso(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat()
