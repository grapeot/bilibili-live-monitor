"""礼物目录快照、按需素材缓存与固定版本；不触发送礼或改变直播页面。"""
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .gift_dynamic import validate_dynamic


def cache_default():
    return Path(os.getenv('XDG_CACHE_HOME', Path.home() / '.cache')) / 'bilibili-live-monitor/gift-assets'


def sha_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.partial-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(canonical(value) + b'\n')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def positive(value):
    if isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) < 2**64:
        raise ValueError('房间、礼物及数量必须为正整数')
    return int(value)


def values(value):
    return list(value.values()) if isinstance(value, dict) else value


def normalize_catalog(payload):
    source = payload.get('assets', payload)
    gifts = values(source.get('gifts', source.get('giftAssets', [])))
    effects = values(source.get('effects', source.get('effectAssetsList', [])))
    if not isinstance(gifts, list) or not isinstance(effects, list):
        raise ValueError('目录需要gifts/effects列表或网页giftAssets/effectAssetsList快照')
    return {'gifts': gifts, 'effects': effects}


def select_effect(catalog, gift_id, count):
    gift_id, count = positive(gift_id), positive(count)
    matches = [g for g in catalog['gifts'] if int(g['id']) == gift_id]
    if len(matches) != 1:
        raise ValueError('目录中不能唯一定位礼物')
    gift = matches[0]
    # Exact quantity tiers override the ordinary effect; zero means use the ordinary effect.
    tiers = [tier for tier in gift.get('countMap', []) if int(tier['num']) == count]
    if len(tiers) > 1:
        raise ValueError('礼物数量档位重复')
    effect_id = (tiers[0].get('effectId') if tiers else None) or gift.get('effectId', 0)
    matches = [e for e in catalog['effects'] if int(e['effectId']) == int(effect_id)]
    if not effect_id or len(matches) != 1:
        raise ValueError('该礼物/数量没有可唯一定位的特效')
    effect = matches[0]
    if not effect.get('webMP4') or not effect.get('webMP4Json'):
        raise ValueError('第一版仅支持MP4及配套JSON；此特效格式暂不支持')
    return gift, effect


def validate_config(config, packed_size=None):
    info = config.get('info', {})
    for key in ('w', 'h', 'f', 'fps', 'videoW', 'videoH'):
        value = info.get(key)
        if type(value) is not int or not 0 < value <= (36000 if key == 'f' else 8192):
            raise ValueError('特效JSON尺寸、帧数或帧率无效')
    if info['fps'] > 120 or info['f'] / info['fps'] > 300:
        raise ValueError('特效时长或帧率超限')
    for key in ('rgbFrame', 'aFrame'):
        rect = info.get(key)
        if (not isinstance(rect, list) or len(rect) != 4 or any(type(x) is not int for x in rect)
                or min(rect[:2]) < 0 or min(rect[2:]) <= 0
                or rect[0] + rect[2] > info['videoW'] or rect[1] + rect[3] > info['videoH']):
            raise ValueError('特效裁剪区域超出素材画布')
    if info['rgbFrame'][2:] != [info['w'], info['h']]:
        raise ValueError('不支持彩色裁剪区域与逻辑尺寸不同的特效')
    if packed_size is not None and tuple(packed_size) != (info['videoW'], info['videoH']):
        raise ValueError('实际素材尺寸与特效JSON不符')
    validate_dynamic(config)
    return info


def download(url, limit):
    # Only public Bilibili CDN assets are part of this cache, never arbitrary private URLs.
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for _ in range(4):
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or
                    not parsed.hostname.endswith('.hdslb.com') or parsed.username or parsed.password):
                raise ValueError('礼物素材必须来自HTTPS hdslb.com CDN')
            with client.stream('GET', url) as response:
                if response.is_redirect:
                    from urllib.parse import urljoin
                    url = urljoin(url, response.headers['location'])
                    continue
                response.raise_for_status()
                result = bytearray()
                for chunk in response.iter_bytes():
                    result.extend(chunk)
                    if len(result) > limit:
                        raise ValueError('礼物资源超出下载大小上限')
                return bytes(result)
    raise ValueError('礼物资源重定向次数过多')


async def browser_catalog(cdp_url, room):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        pages = [page for context in browser.contexts for page in context.pages
                 if urlsplit(page.url).hostname == 'live.bilibili.com'
                 and urlsplit(page.url).path.rstrip('/') == '/' + str(positive(room))]
        if len(pages) != 1:
            raise ValueError('请指定已打开且唯一的直播页面，或使用--catalog本地目录快照')
        payload = await pages[0].evaluate('''() => {
          const gift = document.querySelector('#gift-screen-animation-vm')?.__vue__?.$store?.state?.gift;
          if (!gift) throw new Error('网页礼物目录尚未载入或结构已变化');
          return {gifts:gift.giftAssets,effects:gift.effectAssetsList};
        }''')
        # Exiting Playwright disconnects this client; do not close the shared browser/page.
        return normalize_catalog(payload)


def index_path(cache, room, gift_id, count, effect_id=None):
    suffix = '' if effect_id is None else f'-effect-{positive(effect_id)}'
    return Path(cache) / 'entries' / f'{positive(room)}-{positive(gift_id)}-{positive(count)}{suffix}.json'


def load_asset(path):
    path = Path(path).resolve(strict=True)
    asset = json.loads(path.read_text())
    if asset.get('schema_version') != 1 or asset.get('format') != 'packed-mp4':
        raise ValueError('不支持的礼物缓存版本')
    for key in ('media', 'config'):
        descriptor = asset[key]
        target = Path(descriptor['path']).resolve(strict=True)
        if sha_file(target) != descriptor['sha256']:
            raise ValueError('礼物缓存指纹不符：' + key)
    config = json.loads(Path(asset['config']['path']).read_text())
    validate_config(config)
    return asset


def cached_asset(cache, room, gift_id, count, effect_id=None):
    index = json.loads(index_path(cache, room, gift_id, count, effect_id).read_text())
    path = Path(cache) / 'versions' / (index['version'] + '.json')
    asset = load_asset(path)
    if (asset['room_id'], asset['gift_id'], asset['count']) != (positive(room), positive(gift_id), positive(count)):
        raise ValueError('缓存索引不属于所请求的房间/礼物/数量')
    if effect_id is not None and asset['effect_id'] != positive(effect_id):
        raise ValueError('缓存素材与事件动画ID不符')
    return path.resolve(), asset


def fetch(cache, room, gift_id, count=1, *, catalog=None, cdp_url=None, refresh=False,
          effect_id=None, gift_name=None):
    cache = Path(cache).resolve()
    room, gift_id, count = positive(room), positive(gift_id), positive(count)
    index = index_path(cache, room, gift_id, count, effect_id)
    if index.exists() and not refresh:
        path, asset = cached_asset(cache, room, gift_id, count, effect_id)
        return {'cache_hit': True, 'asset': str(path), 'version': asset['version']}
    if catalog is not None:
        snapshot = normalize_catalog(json.loads(Path(catalog).read_text()))
    elif cdp_url:
        snapshot = asyncio.run(browser_catalog(cdp_url, room))
    else:
        raise ValueError('缓存缺失；请提供--catalog或--cdp-url，离线渲染不会自动访问网页')
    if effect_id is None:
        gift, effect = select_effect(snapshot, gift_id, count)
    else:
        matches = [e for e in snapshot['effects'] if int(e['effectId']) == positive(effect_id)]
        if len(matches) != 1 or not matches[0].get('webMP4') or not matches[0].get('webMP4Json'):
            raise ValueError('事件动画不存在、重复或格式暂不支持')
        gift, effect = {'name': gift_name}, matches[0]
    observed = datetime.now(timezone.utc).isoformat()
    snapshot_id = hashlib.sha256(canonical(snapshot)).hexdigest()
    write_json(cache / 'catalogs' / f'{snapshot_id}.json', snapshot)
    objects = {}
    for key, url, suffix, limit in (
            ('config', effect['webMP4Json'], '.json', 2 * 1024**2),
            ('media', effect['webMP4'], '.mp4', 64 * 1024**2)):
        body = download(url, limit)
        if key == 'config':
            validate_config(json.loads(body))
        digest = hashlib.sha256(body).hexdigest()
        path = cache / 'objects' / (digest + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            fd, name = tempfile.mkstemp(prefix='.partial-', dir=path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(body)
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass  # Another fetch may have installed the same content-addressed object.
            finally:
                temporary.unlink(missing_ok=True)
        if sha_file(path) != digest:
            raise ValueError('已有缓存对象损坏，不覆盖')
        objects[key] = {'path': str(path), 'sha256': digest, 'url': url}
    version_data = {'schema_version': 1, 'format': 'packed-mp4', 'room_id': room, 'gift_id': gift_id,
                    'count': count, 'gift_name': gift['name'], 'effect_id': effect['effectId'],
                    'catalog_sha256': snapshot_id, **objects}
    version = hashlib.sha256(canonical(version_data)).hexdigest()
    asset = {**version_data, 'version': version, 'observed_at_utc': observed}
    path = cache / 'versions' / (version + '.json')
    if not path.exists():
        write_json(path, asset)
    load_asset(path)
    write_json(index, {'version': version})
    return {'cache_hit': False, 'asset': str(path), 'version': version}


def configure_parser(commands):
    parser = commands.add_parser('gift-assets', help='按需缓存B站礼物动画；只读网页配置')
    actions = parser.add_subparsers(dest='action', required=True)
    for action in ('fetch', 'fetch-event'):
        item = actions.add_parser(action)
        item.add_argument('--cache', type=Path, default=cache_default())
        if action == 'fetch':
            item.add_argument('--room', required=True)
            item.add_argument('--gift-id', required=True)
            item.add_argument('--count', type=int, default=1)
        else:
            item.add_argument('--session', type=Path, required=True)
            item.add_argument('--event-id', required=True)
        sources = item.add_mutually_exclusive_group()
        sources.add_argument('--catalog', type=Path)
        sources.add_argument('--cdp-url')
        item.add_argument('--refresh', action='store_true')
        if action == 'fetch-event':
            item.add_argument('--include-recorded-tail', action='store_true',
                              help='允许绑定cutoff后同一连续录制内的尾段事件')
            item.add_argument('--card-background', type=Path, help='已核对来源的结果卡背景PNG')
            item.add_argument('--card-digits', type=Path, help='0.png至9.png的平台数字素材目录')


def main(args):
    try:
        if args.action == 'fetch-event':
            from .gift_event import guard_event, is_blind_event
            if is_blind_event(args.session, args.event_id):
                from .gift_blind import fetch_event
                result = fetch_event(args.cache, args.session, args.event_id, catalog=args.catalog,
                                     cdp_url=args.cdp_url, refresh=args.refresh,
                                     card_background=args.card_background, card_digits=args.card_digits,
                                     include_recorded_tail=getattr(args, 'include_recorded_tail', False))
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            event = guard_event(args.session, args.event_id,
                                include_recorded_tail=getattr(args, 'include_recorded_tail', False))
            result = fetch(args.cache, event['room_id'], event['gift_id'], event['count'],
                           effect_id=event['effect_id'], gift_name=event['gift_name'],
                           catalog=args.catalog, cdp_url=args.cdp_url, refresh=args.refresh)
            result['event'] = event
        else:
            result = fetch(args.cache, args.room, args.gift_id, args.count,
                           catalog=args.catalog, cdp_url=args.cdp_url, refresh=args.refresh)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, httpx.HTTPError) as error:
        print(json.dumps({'error_type': type(error).__name__, 'error': str(error)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
