"""批次录像只播区间并集；重复投递去重，真实赠送与各自触发时刻全部保留。"""
import copy
import json
import sqlite3
import subprocess
from argparse import Namespace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from test_gift_render import media_fixture  # noqa: F401

from bilibili_live_monitor import gift_batch
from bilibili_live_monitor.gift_media import absolute_time
from bilibili_live_monitor.trailer import run


@pytest.mark.parametrize('windows,expected', [
    ([(0, 2), (1, 3), (2.9, 4)], [(0, 4)]),
    ([(0, 2), (2, 3)], [(0, 3)]),
    ([(0, 2), (2.1, 3)], [(0, 2), (2.1, 3)]),
    ([(0, 4), (1, 2)], [(0, 4)]),
])
def test_window_union_including_transitive_overlap(windows, expected):
    groups = gift_batch.merge_windows([{'event_id': i, 'start': a, 'end': b}
                                       for i, (a, b) in enumerate(windows)])
    assert [(g['start'], g['end']) for g in groups] == expected
    assert sum(len(g['events']) for g in groups) == len(windows)


@pytest.fixture
def batch_case(tmp_path, media_fixture):  # noqa: F811 - pytest按名注入复用的媒体fixture。
    single, base = media_fixture
    session = tmp_path/'session'
    (session/'offline').mkdir(parents=True)
    database = tmp_path/'monitor.sqlite3'
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE timeline (id INTEGER,room_id TEXT,kind TEXT,at_utc TEXT,source_key TEXT,data TEXT)')
        for ident, t, key in [(1, 1, 'gift:one'), (2, 1.2, 'gift:two'), (3, 1.4, 'gift:three'),
                               (4, 1.6, 'gift:four'), (5, 1, 'gift:one'), (6, 2, 'gift:no-effect'),
                               (7, 2, 'gift:blind')]:
            gift = {'event_type': 'SEND_GIFT_V2', 'gift_id': '123', 'gift_name': '测试礼物',
                    'num': 1, 'sender_uid': '999'}
            if ident == 6:
                gift.update(gift_id='456', gift_name='无特效礼物')
            if ident == 7:
                gift['blind_gift'] = {'original_gift_id': 789}
            db.execute('INSERT INTO timeline VALUES (?,?,?,?,?,?)',
                       (ident, '123456', 'gift', (start+timedelta(seconds=t)).isoformat(), key,
                        json.dumps({'gift': gift})))
    (session/'config.json').write_text(json.dumps({'database': str(database), 'room_id': '123456',
                                                  'start_utc': start.isoformat()}))
    (session/'offline/manifest.json').write_text(json.dumps({'cutoff': start.timestamp()+4, 'rows': [
        {'path': str(base), 'wall_start': start.timestamp(), 'duration_sec': 4,
         'size': base.stat().st_size, 'mtime_ns': base.stat().st_mtime_ns}]}))
    args = Namespace(session=session, cache=single.asset.parents[1], catalog=None, font=None,
                     canvas='128x96', before=.2, after=.2, gap=0, event_aspect=[],
                     event_region=['1=0,0,64,48', '2=64,0,64,48', '3=0,48,64,48', '4=64,48,64,48'])
    return args, database


def test_four_real_events_one_source_window_and_duplicate_delivery_once(batch_case, tmp_path):
    args, _ = batch_case
    plan = gift_batch.make_batch(args)
    assert len(plan['scenes']) == 1
    scene = plan['scenes'][0]
    assert plan['duration'] == pytest.approx(2)
    assert plan['naive_clip_seconds'] == pytest.approx(5.6)
    assert [e['event_id'] for e in scene['overlays']] == [1, 2, 3, 4]
    assert [e['overlay']['at'] for e in scene['overlays']] == pytest.approx([.2, .4, .6, .8])
    duplicate = next(e for e in plan['inventory'] if e['event_id'] == 5)
    assert duplicate['status'] == 'duplicate' and duplicate['duplicate_of'] == 1
    output = tmp_path/'batch'
    result = gift_batch.render_batch(plan, output)
    assert result['verified'] and result['rendered_events'] == 4 and result['duplicate_events'] == 1
    assert result['uncovered_events'] == 2 and result['source_seconds'] == pytest.approx(2)
    assert len(list(output.glob('scene-*'))) == 1
    # 四次触发都真实出现：同一底片时刻的四个不同区域均有特效，不是只保留第一个。
    frame = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', '0.9', '-i', str(output/'video.mp4'),
                                     '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
    pixels = np.frombuffer(frame, dtype=np.uint8).reshape(96, 128, 3)
    for x, y in [(32, 24), (96, 24), (32, 72), (96, 72)]:
        red, _, blue = pixels[y, x]
        assert red > 80 and blue > 80
    metadata = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_chapters',
                               '-of', 'json', str(output/'video.mp4')]).stdout)
    video = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
    assert int(video['nb_frames']) == 60 and len(metadata['chapters']) == 1
    # 场景叠加阶段直接复制已准备底片的音频，不为每个特效重复编码/播放。
    def audio_hash(path):
        return run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                    '-c:a', 'copy', '-f', 'hash', '-']).stdout
    assert audio_hash(output/'scene-0000/base.mp4') == audio_hash(output/'scene-0000/video.mp4')
    with pytest.raises(FileExistsError):
        gift_batch.render_batch(plan, output)


def test_same_gift_sender_and_timestamp_with_different_identity_survives(batch_case):
    args, database = batch_case
    with sqlite3.connect(database) as db:
        row = db.execute('SELECT room_id,kind,at_utc,data FROM timeline WHERE id=1').fetchone()
        db.execute('INSERT INTO timeline VALUES (?,?,?,?,?,?)',
                   (8, row[0], row[1], row[2], 'gift:another-real-order', row[3]))
    plan = gift_batch.make_batch(args)
    assert len(plan['scenes'][0]['overlays']) == 5


def test_duplicate_key_with_different_quantity_is_a_conflict(batch_case):
    args, database = batch_case
    with sqlite3.connect(database) as db:
        gift = json.loads(db.execute('SELECT data FROM timeline WHERE id=5').fetchone()[0])
        gift['gift']['num'] = 2
        db.execute('UPDATE timeline SET data=? WHERE id=5', (json.dumps(gift),))
    with pytest.raises(ValueError, match='身份的内容冲突'):
        gift_batch.make_batch(args)


def test_empty_renderable_batch_still_reports_inventory(batch_case, tmp_path):
    args, database = batch_case
    args.event_region = []
    with sqlite3.connect(database) as db:
        db.execute('DELETE FROM timeline WHERE id<6')
    plan = gift_batch.make_batch(args)
    assert not plan['scenes'] and len(plan['inventory']) == 2
    output = tmp_path/'empty'
    with pytest.raises(ValueError, match='inventory'):
        gift_batch.render_batch(plan, output)
    assert not output.exists()


def test_batch_rejects_dropped_repeated_or_overlapping_events(batch_case):
    args, _ = batch_case
    plan = gift_batch.make_batch(args)
    for mode in ('drop', 'repeat', 'replay', 'time'):
        edited = copy.deepcopy(plan)
        if mode == 'drop':
            edited['scenes'][0]['overlays'].pop()
        elif mode == 'repeat':
            edited['scenes'][0]['overlays'].append(edited['scenes'][0]['overlays'][0])
        elif mode == 'replay':
            edited['scenes'].append(copy.deepcopy(edited['scenes'][0]))
        else:
            edited['scenes'][0]['overlays'][0]['overlay']['at'] += .05
        with pytest.raises(ValueError, match='覆盖|重叠|时间'):
            gift_batch.validate_batch(edited)


def test_disjoint_scenes_preserve_gaps_as_cuts_not_replay(batch_case, tmp_path):
    args, database = batch_case
    args.event_region = []
    with sqlite3.connect(database) as db:
        db.execute('DELETE FROM timeline WHERE id IN (3,4)')
        start = absolute_time('2030-01-01T00:00:00Z')
        at = datetime.fromtimestamp(start+2.6, timezone.utc).isoformat()
        db.execute('UPDATE timeline SET at_utc=? WHERE id=2', (at,))
    plan = gift_batch.make_batch(args)
    assert len(plan['scenes']) == 2 and plan['duration'] == pytest.approx(2.8)
    result = gift_batch.render_batch(plan, tmp_path/'separate')
    assert result['verified'] and result['scenes'] == 2
    pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', result['video'],
                                    '-map', '0:a:0', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'])
    samples = np.frombuffer(pcm, dtype='<f4')
    # 两段正弦音在1.4秒拼接；逐段AAC原封拼接会在此引入额外静音。
    windows = [samples[int(t*48000):int((t+.005)*48000)] for t in np.arange(1.35, 1.45, .005)]
    assert min(float(np.sqrt(np.mean(w**2))) for w in windows) > .025


@pytest.mark.parametrize('mutation', ['shift', 'copy'])
def test_actual_source_offsets_cannot_replay_other_scene(batch_case, tmp_path, mutation):
    args, database = batch_case
    args.event_region = []
    with sqlite3.connect(database) as db:
        db.execute('DELETE FROM timeline WHERE id IN (3,4)')
        db.execute('UPDATE timeline SET at_utc=? WHERE id=2', ('2030-01-01T00:00:02.600000+00:00',))
    plan = gift_batch.make_batch(args)
    assert len(plan['scenes']) == 2
    if mutation == 'shift':
        plan['scenes'][1]['clips'][0]['start'] += .1
    else:
        plan['scenes'][1]['clips'] = copy.deepcopy(plan['scenes'][0]['clips'])
    output = tmp_path/'invalid'
    with pytest.raises(ValueError, match='取片路径或偏移'):
        gift_batch.render_batch(plan, output)
    assert not output.exists()


def test_source_manifest_is_frozen(batch_case):
    args, _ = batch_case
    plan = gift_batch.make_batch(args)
    manifest = args.session/'offline/manifest.json'
    manifest.write_text(manifest.read_text()+'\n')
    with pytest.raises(ValueError, match='清单已改变'):
        gift_batch.validate_batch(plan)


def test_five_orders_after_cutoff_reported_then_explicitly_rendered(batch_case, tmp_path):
    args, database = batch_case
    args.event_region = []
    manifest = args.session/'offline/manifest.json'
    value = json.loads(manifest.read_text())
    origin = absolute_time('2030-01-01T00:00:00Z')
    value['cutoff'] = origin+1.5
    manifest.write_text(json.dumps(value))
    original = manifest.read_bytes()
    with sqlite3.connect(database) as db:
        gift = db.execute('SELECT data FROM timeline WHERE id=1').fetchone()[0]
        db.execute('DELETE FROM timeline')
        for ident, second in [(1,.8),(8,1.6),(9,1.7),(10,1.9),(11,1.9),(12,2)]:
            db.execute('INSERT INTO timeline VALUES (?,?,?,?,?,?)',
                       (ident,'123456','gift',datetime.fromtimestamp(origin+second,timezone.utc).isoformat(),
                        f'unique-order:{ident}',gift))
    report = gift_batch.make_batch(args)
    assert [e['event_id'] for e in report['inventory']] == [1]
    assert [e['event_id'] for e in report['recorded_tail']['events']] == [8,9,10,11,12]
    assert report['recorded_tail']['included'] is False
    args.include_recorded_tail = True
    included = gift_batch.make_batch(args)
    assert len(included['inventory']) == 6
    assert all(e['status']=='planned' for e in included['inventory']), [(e['event_id'],e['status'],e.get('reason')) for e in included['inventory']]
    assert included['recorded_tail']['included'] is True
    result = gift_batch.render_batch(included,tmp_path/'with-tail')
    assert result['verified'] and result['rendered_events']==6
    assert len(result['recorded_tail']['events'])==5
    assert manifest.read_bytes()==original
    edited = copy.deepcopy(included)
    edited['inventory'] = [e for e in edited['inventory'] if e['event_id']!=12]
    for scene in edited['scenes']:
        scene['overlays'] = [e for e in scene['overlays'] if e['event_id']!=12]
    with pytest.raises(ValueError,match='尾段礼物'):
        gift_batch.validate_batch(edited)


@pytest.mark.parametrize('next_start,next_group,expected,reason', [
    (2,'same',3,'recording_end'),
    (2.1,'same',2,'recording_gap_or_overlap'),
    (1.8,'same',1.8,'recording_gap_or_overlap'),
    (2,'next',2,'different_recording_run'),
])
def test_tail_stops_at_gap_overlap_or_other_capture(batch_case,next_start,next_group,expected,reason):
    args,_ = batch_case
    from bilibili_live_monitor.gift_scope import recorded_tail
    path = args.session/'offline/manifest.json'
    value = json.loads(path.read_text())
    origin = value['rows'][0]['wall_start']
    row = value['rows'][0]
    value.update(cutoff=origin+1.5,rows=[
        {**row,'duration_sec':2,'run_group':'same'},
        {**row,'wall_start':origin+next_start,'duration_sec':1,'run_group':next_group}])
    path.write_text(json.dumps(value))
    scope = recorded_tail(args.session)
    assert scope['recorded_end']==origin+expected
    assert scope['stop_reason']==reason


def test_no_tail_when_cutoff_is_in_recording_gap(batch_case):
    args,_ = batch_case
    from bilibili_live_monitor.gift_scope import recorded_tail
    path = args.session/'offline/manifest.json'
    value = json.loads(path.read_text())
    value['cutoff'] += 1
    path.write_text(json.dumps(value))
    scope = recorded_tail(args.session)
    assert scope['recorded_end']==value['cutoff']
    assert scope['stop_reason']=='no_recording_at_cutoff'


@pytest.mark.parametrize('same_run', [True,False])
def test_cutoff_at_capture_boundary_uses_preceding_run(batch_case,same_run):
    args,_ = batch_case
    from bilibili_live_monitor.gift_scope import recorded_tail
    path = args.session/'offline/manifest.json'
    value = json.loads(path.read_text())
    row = value['rows'][0]
    origin = row['wall_start']
    value.update(cutoff=origin+2,rows=[{**row,'duration_sec':2,'run_group':'first'},
        {**row,'wall_start':origin+2,'duration_sec':2,'run_group':'first' if same_run else 'next'}])
    path.write_text(json.dumps(value))
    scope = recorded_tail(args.session)
    assert scope['recorded_end']==origin+(4 if same_run else 2)


def test_tail_effect_cannot_take_context_from_next_recording_run(batch_case):
    args,database = batch_case
    args.event_region = []
    args.include_recorded_tail = True
    path = args.session/'offline/manifest.json'
    value = json.loads(path.read_text())
    row = value['rows'][0]
    origin = row['wall_start']
    value.update(cutoff=origin+1,rows=[{**row,'duration_sec':2,'run_group':'first'},
        {**row,'wall_start':origin+2,'duration_sec':2,'run_group':'next'}])
    path.write_text(json.dumps(value))
    with sqlite3.connect(database) as db:
        db.execute('DELETE FROM timeline WHERE id!=1')
        db.execute('UPDATE timeline SET at_utc=? WHERE id=1',
                   (datetime.fromtimestamp(origin+1.5,timezone.utc).isoformat(),))
    plan = gift_batch.make_batch(args)
    assert plan['inventory'][0]['status']=='uncovered'
    assert '不能跨场' in plan['inventory'][0]['reason']
    assert not plan['scenes']
