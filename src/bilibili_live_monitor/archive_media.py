"""本地归档文件定位。旧来源身份保留，压缩副本不冒充原片。"""
import json
from pathlib import Path


def archive_path(source):
    source = Path(source).resolve()
    return source.parent / 'archive' / (source.name + '.mp4')


def receipt_path(source):
    return archive_path(source).with_suffix('.json')


def file_stat(path):
    stat = Path(path).stat()
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def archive_receipt(source):
    source = Path(source).resolve()
    target = archive_path(source)
    saved = json.loads(receipt_path(source).read_text())
    if (saved.get('status') != 'archived' or saved.get('source') != str(source)
            or saved.get('output') != str(target) or not target.is_file()
            or saved.get('output_stat') != file_stat(target)):
        raise ValueError('归档副本或完成记录不匹配：' + str(source))
    return saved


def resolve_media(source, *, expected_sha=None, video=False):
    """原片优先；原片缺失时，只使用已验收的固定位置副本。"""
    source = Path(source).resolve()
    if source.is_file():
        if video:
            from .media_codec import prepare_video
            return prepare_video(source, expected_sha=expected_sha)[0]
        return source
    saved = archive_receipt(source)
    if expected_sha is not None and saved['legacy_row'].get('sha256') != expected_sha:
        raise ValueError('归档记录与请求的旧来源不一致')
    return archive_path(source)


def archived_input(row):
    """核对旧来源的大小/时间身份；不将旧SHA用于验证压缩副本。"""
    saved = archive_receipt(row['path'])
    expected = {key: row[key] for key in ('size', 'mtime_ns')}
    if saved['source_stat'] != expected:
        raise ValueError('归档记录不属于这份旧来源：' + row['path'])
    if saved['legacy_row'].get('sha256') != row.get('sha256'):
        raise ValueError('归档记录与请求的旧来源不一致')
    return archive_path(row['path'])


def source_paths(root, suffixes, *, recursive=False):
    """扫描时把已迁移原片作为逻辑输入列一次，不重复列压缩副本。"""
    root = Path(root).resolve()
    files = root.rglob('*') if recursive else root.iterdir()
    paths = {p for p in files if p.is_file() and p.suffix.lower() in suffixes
             and '.codec-media' not in p.relative_to(root).parts}
    pattern = '**/archive/*.mkv.json' if recursive else 'archive/*.mkv.json'
    for receipt in root.glob(pattern):
        # 只按固定文件名枚举，留给本场筛选后的读取去验收。
        # 不能让另一旧场的坏回执阻断当前直播的离线处理。
        source = receipt.parent.parent / receipt.name.removesuffix('.json')
        if not source.exists():
            paths.add(source)
    return sorted(paths)
