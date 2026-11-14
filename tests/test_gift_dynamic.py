"""使用合成VAP素材验证文字槽、移动遮罩和不同帧率，不依赖真实直播素材。"""
import copy
import json
import shutil
import sqlite3
import subprocess
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest
from PIL import ImageFont

from bilibili_live_monitor import gift_assets, gift_render
from bilibili_live_monitor.gift_dynamic import USERNAME_TAG, render_rgba
from bilibili_live_monitor.gift_event import guard_event
from bilibili_live_monitor.gift_media import absolute_time
from bilibili_live_monitor.trailer import run


@pytest.fixture
def font_path():
    for name in ['DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                 '/System/Library/Fonts/Supplemental/Arial.ttf']:
        try:
            return Path(ImageFont.truetype(name, 12).path).resolve()
        except OSError:
            continue
    pytest.skip('requires a local TrueType font')


def dynamic_config():
    return {'info': {'w': 64, 'h': 64, 'f': 30, 'fps': 30, 'videoW': 96, 'videoH': 64,
                     'rgbFrame': [0, 0, 64, 64], 'aFrame': [64, 0, 32, 32], 'custom': 1},
            'src': [{'srcId': 'name', 'srcType': 'txt', 'srcTag': USERNAME_TAG, 'fitType': 'fitXY',
                     'color': '#ffffff', 'style': '', 'w': 32, 'h': 24}],
            'frame': [{'i': i, 'obj': [{'srcId': 'name', 'z': 0,
                       'frame': [4 if i < 15 else 28, 20, 32, 24],
                       'mFrame': [64, 0 if 10 <= i < 15 else 32, 32, 24]}]} for i in range(5, 25)]}


@pytest.mark.parametrize('change', ['image', 'unknown_tag', 'style', 'mask_bounds', 'frame_bounds',
                                   'duplicate_frame', 'missing_source', 'slot_size'])
def test_unsupported_dynamic_data_is_not_silently_omitted(change):
    config = dynamic_config()
    if change == 'image':
        config['src'][0]['srcType'] = 'img'
    elif change == 'unknown_tag':
        config['src'][0]['srcTag'] = 'unknown'
    elif change == 'style':
        config['src'][0]['style'] = 'unimplemented'
    elif change == 'mask_bounds':
        config['frame'][0]['obj'][0]['mFrame'][0] = 95
    elif change == 'frame_bounds':
        config['frame'][0]['obj'][0]['frame'][0] = 63
    elif change == 'duplicate_frame':
        config['frame'].append(copy.deepcopy(config['frame'][0]))
    elif change == 'missing_source':
        config['frame'][0]['obj'][0]['srcId'] = 'missing'
    else:
        config['src'][0]['w'] = None
    with pytest.raises(ValueError, match='动态'):
        gift_assets.validate_config(config)


@pytest.fixture
def packed(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('requires ffmpeg/ffprobe')
    path = tmp_path / 'packed.mp4'
    run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=64x64:r=30:d=1',
         '-f', 'lavfi', '-i', 'color=black:s=32x32:r=30:d=1', '-f', 'lavfi', '-i',
         'color=white:s=32x32:r=30:d=1', '-filter_complex', '[1:v][2:v]vstack[a];[0:v][a]hstack[v]',
         '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)])
    return path


def test_dynamic_text_follows_frame_positions_and_mask(tmp_path, packed, font_path):
    result = render_rgba(packed, dynamic_config(), 'AB', font_path, tmp_path / 'alpha.mov')
    raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(result),
                                   '-f', 'rawvideo', '-pix_fmt', 'rgba', '-'])
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(30, 64, 64, 4)
    assert frames[0, :, :, 3].max() == 0
    assert frames[11, :, :, 3].max() == 0  # 黑色mFrame完全隐藏昵称。
    assert frames[29, :, :, 3].max() == 0
    early = np.nonzero(frames[7, :, :, 3] > 100)
    later = np.nonzero(frames[18, :, :, 3] > 100)
    assert len(early[0]) > 10 and len(later[0]) > 10
    assert float(later[1].mean()-early[1].mean()) == pytest.approx(24, abs=.1)


@pytest.fixture
def dynamic_asset(tmp_path, packed, monkeypatch):
    catalog = tmp_path / 'catalog.json'
    # 舰长无需出现在普通gift列表中；素材ID来自绑定消息，不能按礼物ID猜。
    catalog.write_text(json.dumps({'gifts': [], 'effects': [
        {'effectId': 777, 'webMP4': 'https://i0.hdslb.com/test.mp4',
         'webMP4Json': 'https://i0.hdslb.com/test.json'}]}))
    monkeypatch.setattr(gift_assets, 'download', lambda url, limit:
                        json.dumps(dynamic_config()).encode() if url.endswith('.json') else packed.read_bytes())
    cache = tmp_path / 'cache'
    first = gift_assets.fetch(cache, 123456, 800, 6, effect_id=777, gift_name='测试舰长', catalog=catalog)
    monkeypatch.setattr(gift_assets, 'download', lambda *a: pytest.fail('cache hit must not fetch'))
    hit = gift_assets.fetch(cache, 123456, 800, 6, effect_id=777)
    assert hit['cache_hit'] and hit['version'] == first['version']
    assert not gift_assets.index_path(cache, 123456, 800, 6).exists()
    return Path(first['asset'])


@pytest.mark.parametrize('fps', [25, 30])
def test_real_dynamic_render_time_and_font_binding(tmp_path, dynamic_asset, font_path, fps):
    base = tmp_path / 'base.mp4'
    run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=blue:s=64x64:r={fps}:d=4',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-c:v', 'libx264',
         '-c:a', 'aac', '-shortest', str(base)])
    args = Namespace(asset=dynamic_asset, before=.4, after=.4, base=base, session=None, at='1',
                     base_display_aspect=None, canvas=None, region=None, placement='center-fit',
                     font=font_path, username='AB')
    plan = gift_render.make_plan(args)
    assert plan['fps'] == 30 and plan['duration'] == pytest.approx(1.8)
    assert plan['dynamic']['font']['sha256'] == gift_assets.sha_file(font_path)
    output = tmp_path / 'render'
    assert gift_render.render(plan, output)['verified']
    data = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
                           '-show_entries', 'packet=pts_time', '-of', 'json', str(output/'video.mp4')]).stdout)
    pts = sorted(float(p['pts_time']) for p in data['packets'])
    assert len(pts) == 54
    assert max(abs(t-i/30) for i, t in enumerate(pts)) < .00001
    for t, visible in [(.1, False), (.65, True), (1.6, False)]:
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', str(t), '-i', str(output/'video.mp4'),
                                       '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        image = np.frombuffer(raw, dtype=np.uint8).reshape(64, 64, 3)
        assert bool(((image[:, :, 0] > 100) & (image[:, :, 1] > 100)).any()) == visible
    def audio_hash(path):
        return run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                    '-c:a', 'copy', '-f', 'hash', '-']).stdout
    assert audio_hash(output/'base.mp4') == audio_hash(output/'video.mp4')
    edited = copy.deepcopy(plan)
    edited['dynamic']['font']['sha256'] = 'changed'
    with pytest.raises(ValueError, match='字体已改变'):
        gift_render.validate_plan(edited)
    args.font = None
    with pytest.raises(ValueError, match='--font'):
        gift_render.make_plan(args)


@pytest.fixture
def guard_session(tmp_path):
    database = tmp_path/'monitor.sqlite3'
    at = '2030-01-01T00:00:05+00:00'
    stamp = int(absolute_time(at))
    gift = {'event_type': 'GUARD_BUY', 'sender_uid': '999', 'gift_id': '800', 'num': 6,
            'guard_level': 3, 'time_source': 'event', 'gift_name': '测试舰长'}
    raw = {'sender_uinfo': {'uid': 999, 'base': {'name': 'Test User'}},
           'guard_info': {'start_time': stamp, 'guard_level': 3},
           'pay_info': {'num': 6}, 'gift_info': {'gift_id': 800}, 'effect_info': {'ship_effect_id': 777}}
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE timeline (id INTEGER,room_id TEXT,kind TEXT,at_utc TEXT,data TEXT)')
        db.execute('CREATE TABLE raw_events (id INTEGER,room_id TEXT,command TEXT,received_utc TEXT,data TEXT)')
        db.execute('INSERT INTO timeline VALUES (?,?,?,?,?)', (1, '123456', 'gift', at, json.dumps({'gift': gift})))
        db.execute('INSERT INTO raw_events VALUES (?,?,?,?,?)',
                   (2, '123456', 'USER_TOAST_MSG_V2', at, json.dumps({'data': raw})))
    (tmp_path/'config.json').write_text(json.dumps({'database': str(database), 'room_id': '123456',
                                                  'start_utc': '2030-01-01T00:00:00Z'}))
    (tmp_path/'offline').mkdir()
    (tmp_path/'offline/manifest.json').write_text(json.dumps({'cutoff': stamp+10}))
    return tmp_path, database, raw


def test_guard_binding_is_scoped_unique_and_uses_observed_effect(guard_session):
    session, database, raw = guard_session
    bound = guard_event(session, 1)
    assert (bound['effect_id'], bound['username'], bound['count']) == (777, 'Test User', 6)
    assert 'sender_uid' not in bound
    with pytest.raises(ValueError, match='不属于'):
        guard_event(session, 2)
    # 同昵称但另一UID的消息不能替代事件对应的原始消息。
    with sqlite3.connect(database) as db:
        db.execute('DELETE FROM raw_events')
        raw['sender_uinfo']['uid'] = 888
        for ident in (2, 3):
            db.execute('INSERT INTO raw_events VALUES (?,?,?,?,?)',
                       (ident, '123456', 'USER_TOAST_MSG_V2', '2030-01-01T00:00:05+00:00', json.dumps({'data': raw})))
    with pytest.raises(ValueError, match='不能唯一'):
        guard_event(session, 1)
    with sqlite3.connect(database) as db:
        raw['sender_uinfo']['uid'] = 999
        db.execute('UPDATE raw_events SET data=?', (json.dumps({'data': raw}),))
    with pytest.raises(ValueError, match='不能唯一'):
        guard_event(session, 1)


def test_event_plan_cannot_use_other_assets_or_override_username(guard_session, dynamic_asset, font_path):
    session, _, _ = guard_session
    args = Namespace(event_id=1, session=session, asset=dynamic_asset, at=None, count=1, room=None,
                     gift_id=None, username='override')
    with pytest.raises(ValueError, match='不混用'):
        gift_render.make_plan(args)
    args.username = None
    wrong = json.loads(dynamic_asset.read_text())
    wrong['count'] = 1
    args.asset = session/'wrong.json'
    args.asset.write_text(json.dumps(wrong))
    with pytest.raises(ValueError, match='素材不属于'):
        gift_render.make_plan(args)
    args.asset = None
    args.cache = dynamic_asset.parents[1]
    args.before, args.after = .4, .4
    args.font = font_path
    args.base = None
    args.base_display_aspect = args.canvas = args.region = None
    args.placement = 'center-fit'
    base = session/'base.mp4'
    run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=64x64:r=25:d=8',
         '-f', 'lavfi', '-i', 'sine=duration=8', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(base)])
    start = absolute_time('2030-01-01T00:00:00Z')
    (session/'offline/manifest.json').write_text(json.dumps({'cutoff': start+8, 'rows': [
        {'path': str(base), 'wall_start': start, 'duration_sec': 8,
         'size': base.stat().st_size, 'mtime_ns': base.stat().st_mtime_ns}]}))
    plan = gift_render.make_plan(args)
    assert plan['count'] == 6 and plan['event_binding']['raw_event_id'] == 2
    assert plan['dynamic']['username'] == 'Test User'
    assert gift_render.render(plan, session/'render')['verified']
