"""按场次和时间截取弹幕，只作为转写纠错的只读旁证。"""
from .time_utils import epoch


def danmaku_context(source, lower, upper):
    return [{'id': 'm:' + str(data['_record_id']), 'at': at,
             'name': data.get('author', ''), 'text': data.get('text', '')}
            for kind, at, data in source.get('items', [])
            if kind == 'danmaku' and data.get('_record_id') is not None and lower <= epoch(at) <= upper]
