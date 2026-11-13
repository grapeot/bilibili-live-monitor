"""离线合成真实执行小型媒体，覆盖透明度、PK比例与冻结缓存。"""
import json
import shutil
import sqlite3
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from bilibili_live_monitor import gift_assets, gift_render
from bilibili_live_monitor.gift_media import absolute_time, select_rows
from bilibili_live_monitor.trailer import run


def config():
    return {'info': {'w': 64, 'h': 64, 'f': 15, 'fps': 15, 'videoW': 96, 'videoH': 64,
                     'rgbFrame': [0, 0, 64, 64], 'aFrame': [64, 0, 32, 32]}}


def catalog():
    return {'gifts': [{'id': 123, 'name': '测试礼物', 'effectId': 10,
                       'countMap': [{'num': 10, 'effectId': 11}]}],
            'effects': [{'effectId': i, 'webMP4': f'https://i0.hdslb.com/{i}.mp4',
                         'webMP4Json': f'https://i0.hdslb.com/{i}.json'} for i in (10, 11)]}


def test_quantity_tier_and_unsupported():
    assert gift_assets.select_effect(catalog(), 123, 1)[1]['effectId'] == 10
    assert gift_assets.select_effect(catalog(), 123, 10)[1]['effectId'] == 11
    missing = catalog()
    missing['effects'][0]['webMP4'] = ''
    with pytest.raises(ValueError, match='格式暂不支持'):
        gift_assets.select_effect(missing, 123, 1)


def test_cache_pins_versions_and_never_fetches_on_hit(tmp_path, monkeypatch):
    source = tmp_path / 'catalog.json'
    source.write_text(json.dumps(catalog()))
    monkeypatch.setattr(gift_assets, 'download', lambda url, limit:
                        json.dumps(config()).encode() if url.endswith('.json') else b'version-one')
    first = gift_assets.fetch(tmp_path / 'cache', 123456, 123, catalog=source)
    monkeypatch.setattr(gift_assets, 'download', lambda *a: pytest.fail('cache hit must not fetch'))
    hit = gift_assets.fetch(tmp_path / 'cache', 123456, 123)
    assert hit['cache_hit'] and hit['version'] == first['version']
    monkeypatch.setattr(gift_assets, 'download', lambda url, limit:
                        json.dumps(config()).encode() if url.endswith('.json') else b'version-two')
    newer = gift_assets.fetch(tmp_path / 'cache', 123456, 123, catalog=source, refresh=True)
    assert newer['version'] != first['version']
    old = gift_assets.load_asset(first['asset'])
    assert Path(old['media']['path']).read_bytes() == b'version-one'
    Path(old['media']['path']).write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='缓存指纹'):
        gift_assets.load_asset(first['asset'])


def test_config_bounds_and_custom_elements():
    bad = config()
    bad['info']['aFrame'] = [80, 0, 32, 32]
    with pytest.raises(ValueError, match='裁剪区域'):
        gift_assets.validate_config(bad)
    with pytest.raises(ValueError, match='动态替换'):
        gift_assets.validate_config({**config(), 'src': [{'srcId': '1'}]})
    with pytest.raises(ValueError, match='实际素材尺寸'):
        gift_assets.validate_config(config(), (100, 64))


@pytest.mark.parametrize('rows', [
    [{'wall_start': 0, 'duration_sec': 2}, {'wall_start': 3, 'duration_sec': 2}],
    [{'wall_start': 0, 'duration_sec': 3}, {'wall_start': 2, 'duration_sec': 3}],
])
def test_manifest_gap_or_overlap_is_not_collapsed(rows):
    with pytest.raises(ValueError, match='缺口或来源重叠'):
        select_rows(rows, 1, 4)


def test_manifest_contiguous_segments():
    rows = [{'wall_start': 0, 'duration_sec': 2}, {'wall_start': 2, 'duration_sec': 3}]
    assert [(offset, length) for _, offset, length in select_rows(rows, 1, 4)] == [(1, 1), (0, 2)]


def test_session_time_requires_timezone():
    with pytest.raises(ValueError, match='时区'):
        absolute_time('2030-01-01T01:00:00')


def test_events_only_selected_session_and_no_identity_export(tmp_path):
    db_path = tmp_path / 'monitor.sqlite3'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE timeline (id INTEGER, room_id TEXT, kind TEXT, at_utc TEXT, data TEXT)')
        for i, at in enumerate(('2030-01-01T00:00:01+00:00', '2030-01-01T00:00:03+00:00')):
            db.execute('INSERT INTO timeline VALUES (?,?,?,?,?)',
                       (i, '123456', 'gift', at, json.dumps({'sender_uid': 'private',
                        'gift': {'gift_id': '123', 'gift_name': '测试礼物', 'num': 1}})))
    (tmp_path / 'config.json').write_text(json.dumps({'database': str(db_path), 'room_id': '123456',
                                                    'start_utc': '2030-01-01T00:00:00+00:00'}))
    (tmp_path / 'offline').mkdir()
    (tmp_path / 'offline/manifest.json').write_text(json.dumps({'cutoff': absolute_time('2030-01-01T00:00:02Z')}))
    result = gift_render.events(tmp_path)
    assert len(result['events']) == 1 and 'sender_uid' not in json.dumps(result['events'])


@pytest.fixture
def media_fixture(tmp_path, monkeypatch):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('requires ffmpeg/ffprobe')
    base, packed = tmp_path / 'base.mp4', tmp_path / 'effect.mp4'
    run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=blue:s=128x96:r=15:d=4',
         '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
         '-c:a', 'aac', '-shortest', str(base)])
    run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=64x64:r=15:d=1',
         '-f', 'lavfi', '-i', 'color=gray:s=32x64:r=15:d=1', '-filter_complex', 'hstack=inputs=2[v]',
         '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(packed)])
    source = tmp_path / 'catalog.json'
    source.write_text(json.dumps(catalog()))
    monkeypatch.setattr(gift_assets, 'download', lambda url, limit:
                        json.dumps(config()).encode() if url.endswith('.json') else packed.read_bytes())
    asset = gift_assets.fetch(tmp_path / 'cache', 123456, 123, catalog=source)['asset']
    args = Namespace(asset=Path(asset), before=.4, after=.4, base=base, session=None, at='1',
                     base_display_aspect=None, canvas=None, region=None, placement='center-fit')
    return args, base


def test_real_render_transparency_timing_and_readonly_sources(tmp_path, media_fixture):
    args, base = media_fixture
    digest = gift_assets.sha_file(base)
    plan = gift_render.make_plan(args)
    output = tmp_path / 'render'
    result = gift_render.render(plan, output)
    assert result['verified'] and result['canvas'] == [128, 96]
    assert gift_assets.sha_file(base) == digest
    for at, name in ((.1, 'before'), (.8, 'during'), (1.6, 'after')):
        image = tmp_path / (name + '.png')
        run(['ffmpeg', '-v', 'error', '-ss', str(at), '-i', str(output / 'video.mp4'),
             '-frames:v', '1', '-update', '1', str(image)])
        red, _, blue = Image.open(image).convert('RGB').getpixel((64, 48))
        if name == 'during':
            assert red > 80 and blue > 80  # alpha is blended, not an opaque red rectangle
        else:
            assert red < 20 and blue > 200
    with pytest.raises(FileExistsError):
        gift_render.render(plan, output)


def test_legacy_pk_archive_requires_explicit_aspect(tmp_path, media_fixture, monkeypatch):
    args, base = media_fixture
    archive = tmp_path / 'archive'
    archive.mkdir()
    archived = archive / 'sample.mkv.mp4'
    shutil.copyfile(base, archived)
    source = tmp_path / 'sample.mkv'
    receipt = {'status': 'archived', 'source': str(source), 'output': str(archived),
               'output_stat': {'size': archived.stat().st_size, 'mtime_ns': archived.stat().st_mtime_ns},
               'source_info': {'streams': [{'codec_type': 'video', 'width': 128, 'height': 96,
                                            'coded_width': 120, 'coded_height': 96}]}}
    (archive / 'sample.mkv.json').write_text(json.dumps(receipt))
    args.base = archived
    with pytest.raises(ValueError, match='旧归档.*画幅冲突'):
        gift_render.make_plan(args)
    args.base_display_aspect = '5:4'
    plan = gift_render.make_plan(args)
    assert plan['canvas'] == [120, 96]
    assert plan['clips'][0]['aspect_corrected']
    assert gift_render.render(plan, tmp_path / 'corrected')['canvas'] == [120, 96]


def test_plan_rejects_changed_source_and_asset(tmp_path, media_fixture):
    args, base = media_fixture
    plan = gift_render.make_plan(args)
    base.write_bytes(base.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='底层素材已改变'):
        gift_render.render(plan, tmp_path / 'output')
    assert not (tmp_path / 'output').exists()


def test_plan_pins_asset_after_catalog_refresh(tmp_path, media_fixture):
    args, _ = media_fixture
    plan = gift_render.make_plan(args)
    original = gift_render.validate_plan(plan)[0]['version']
    # A newer entry pointer cannot redirect an existing plan.
    entry = next((tmp_path / 'cache/entries').iterdir())
    entry.write_text('{"version":"does-not-exist"}')
    assert gift_render.validate_plan(plan)[0]['version'] == original


def test_cross_segment_pcm_has_no_new_silence_at_join(tmp_path, media_fixture):
    args, base = media_fixture
    second = tmp_path / 'second.mp4'
    shutil.copyfile(base, second)
    session = tmp_path / 'session'
    (session / 'offline').mkdir(parents=True)
    (session / 'config.json').write_text('{"room_id":"123456"}')
    start = absolute_time('2030-01-01T00:00:00Z')
    rows = [{'path': str(p), 'wall_start': start + i * 4, 'duration_sec': 4,
             'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns} for i, p in enumerate((base, second))]
    (session / 'offline/manifest.json').write_text(json.dumps({'rows': rows}))
    args.base, args.session, args.at = None, session, '2030-01-01T00:00:03.9+00:00'
    plan = gift_render.make_plan(args)
    assert len(plan['clips']) == 2
    output = tmp_path / 'joined'
    gift_render.render(plan, output)
    import subprocess
    pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output / 'video.mp4'),
                                   '-vn', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'])
    samples = np.frombuffer(pcm, dtype='<f4')
    # Join is 0.5s into this output. The old per-part AAC path inserted ~25ms silence here.
    windows = [samples[int(t * 48000):int((t + .005) * 48000)] for t in np.arange(.46, .61, .005)]
    assert min(float(np.sqrt(np.mean(window**2))) for window in windows) > .025


def test_expected_cli_error_has_no_traceback(tmp_path, capsys):
    args = Namespace(action='render', plan=None, at=None)
    assert gift_render.main(args) == 1
    captured = capsys.readouterr()
    assert 'Traceback' not in captured.err
    assert json.loads(captured.err)['error_type'] == 'ValueError'
