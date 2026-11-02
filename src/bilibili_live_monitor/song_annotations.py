"""已交叉核对歌名的只读投影；公开展示不携带歌词或来源身份。"""
import math


def merge_segments(segments):
    merged = []
    for segment in sorted(segments, key=lambda s: s['start']):
        if merged and segment['start'] <= merged[-1]['end']:
            merged[-1]['end'] = max(merged[-1]['end'], segment['end'])
        else:
            merged.append(dict(segment))
    return merged


def verify_songs(songs, activity_bands):
    live = [b for b in activity_bands if b['label'] == 'singing']
    seen, spans = set(), []
    for song in songs:
        if song['id'] in seen or not isinstance(song['title'], str) or not song['title'].strip():
            raise ValueError('歌名为空或标识重复')
        seen.add(song['id'])
        if not song['segments'] or len(song['title']) > 120:
            raise ValueError('歌名或区间无效')
        for segment in song['segments']:
            start, end = segment['start'], segment['end']
            if not math.isfinite(start) or not math.isfinite(end) or start >= end:
                raise ValueError('歌曲区间无效')
            covered = sum(max(0, min(end, b['end'])-max(start, b['start'])) for b in live)
            if abs(covered-(end-start)) > 1e-6:
                raise ValueError('歌曲区间不能超出现场演唱候选')
            if any(start < previous_end and end > previous_start for previous_start, previous_end in spans):
                raise ValueError('歌曲区间重叠，歌名归属不明确')
            spans.append((start, end))


def load_songs(annotations, windows, activity_bands, identity):
    if annotations.get('schema_version') != 1 or annotations.get('dataset_id') != identity:
        raise ValueError('歌名标注不属于当前数据集')
    by_key = {w['source_key']: w for w in windows}
    assigned, songs = set(), []
    for row in annotations['songs']:
        if row['confidence'] != 'high':
            continue
        segments = []
        keys = row['source_keys']
        if not keys or len(set(keys)) != len(keys):
            raise ValueError('歌曲窗口为空或重复')
        for key in keys:
            if key not in by_key or key in assigned:
                raise ValueError('歌曲窗口不存在或重复分配')
            window = by_key[key]
            if window['label'] != 'singing' or window['music_source'] != 'probable_live':
                raise ValueError('歌名只能绑定现场演唱候选')
            assigned.add(key)
            for band in activity_bands:
                start, end = max(window['start'], band['start']), min(window['end'], band['end'])
                if band['label'] == 'singing' and start < end:
                    segments.append({'start': start, 'end': end})
        if segments:
            songs.append({'id': f'song-{len(songs)+1}', 'title': row['title'],
                          'segments': merge_segments(segments)})
    verify_songs(songs, activity_bands)
    return songs
