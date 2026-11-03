"""只从本地保存的公开搜索结果扩充歌名，不发布搜索正文或任意模型文本。"""
import fcntl
import hashlib
import json
import re
from pathlib import Path


def normalized(text):
    return re.sub(r'[^\w\u4e00-\u9fff]', '', text).casefold()


def source_matches(title, result):
    return (isinstance(title, str) and 1 <= len(title) <= 60 and title.isprintable()
            and bool(normalized(title)) and isinstance(result, dict)
            and isinstance(result.get('title'), str)
            and normalized(title) in normalized(result['title'])
            and isinstance(result.get('url'), str) and result['url'].startswith('https://')
            and isinstance(result.get('raw_content') or result.get('markdown'), str)
            and bool((result.get('raw_content') or result.get('markdown')).strip()))


def register_source(workspace: Path, title: str, source: Path, index: int) -> dict:
    """登记已由操作者核对歌词的来源；锁内合并，避免覆盖其他曲目。"""
    from .live_share import atomic_json

    raw = source.read_bytes()
    payload = json.loads(raw)
    if type(index) is not int or index < 0 or not source_matches(title, payload['data']['results'][index]):
        raise ValueError('来源标题、HTTPS地址或正文与歌名不匹配')
    if not workspace.is_dir():
        raise ValueError('请先初始化私有工作目录')
    filename = hashlib.sha256(raw).hexdigest()[:24] + '.json'
    with (workspace / 'song_sources.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        root = workspace / 'song_sources'
        root.mkdir(exist_ok=True)
        destination = root / filename
        if not destination.exists() or destination.read_bytes() != raw:
            temporary = root / (filename + '.tmp')
            temporary.write_bytes(raw)
            temporary.replace(destination)
        registry = workspace / 'verified_songs.json'
        entries = json.loads(registry.read_text()) if registry.exists() else []
        if not isinstance(entries, list):
            raise ValueError('歌名登记文件必须为数组')
        entry = {'title': title, 'file': filename, 'result_index': index}
        if entry not in entries:
            entries.append(entry)
        atomic_json(registry, entries)
    return entry


def verified_titles(workspace: Path) -> tuple[str, ...]:
    registry = workspace / 'verified_songs.json'
    if not registry.exists():
        return ()
    try:
        entries = json.loads(registry.read_text())
    except (ValueError, OSError):
        return ()
    titles = set()
    root = (workspace / 'song_sources').resolve()
    for entry in entries if isinstance(entries, list) else []:
        try:
            title = entry['title']
            if not isinstance(title, str) or not 1 <= len(title) <= 60 or not title.isprintable():
                continue
            path = (root / entry['file']).resolve()
            if path.parent != root or path.suffix != '.json':
                continue
            payload = json.loads(path.read_text())
            result = payload['data']['results'][entry['result_index']]
            # A source's page title must actually name this song. Search guesses/snippets alone
            # do not grow the export whitelist; lyrics matching is reviewed by the agent.
            if source_matches(title, result):
                titles.add(title)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError, OSError):
            continue
    return tuple(sorted(titles))
