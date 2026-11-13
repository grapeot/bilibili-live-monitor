"""普通盲盒以原始报文分组，真实数量、资源指纹、移动遮罩和覆盖率均须保持。"""
import base64
import copy
import io
import json
import sqlite3
from argparse import Namespace
from datetime import datetime, timezone

import pytest
from PIL import Image
from test_gift_dynamic import dynamic_config, packed  # noqa: F401

from bilibili_live_monitor import gift_assets, gift_batch, gift_blind, gift_render
from bilibili_live_monitor.danmaku import gift_message
from bilibili_live_monitor.gift_dynamic import BLIND_TAG
from bilibili_live_monitor.gift_protocol import Gift, gift_items
from bilibili_live_monitor.trailer import run


def png(color):
    output = io.BytesIO()
    Image.new('RGBA', (32,32), color).save(output, format='PNG')
    return output.getvalue()


@pytest.fixture
def blind_case(tmp_path, packed, monkeypatch):  # noqa: F811
    session = tmp_path/'session'
    (session/'offline').mkdir(parents=True)
    database = tmp_path/'db.sqlite3'
    start = datetime(2030,1,1,tzinfo=timezone.utc)
    message = Gift(uid=999, uname='Synthetic')
    message.blind_gift.original_gift_id = 789
    message.blind_gift.original_gift_name = '测试盲盒'
    for index, num in enumerate([26,22,2]):
        item = message.items.add(gift_id=120+index, gift_name=f'Result{index}', num=num,
                                 tid=f'order-{index}', timestamp=int(start.timestamp())+1)
        item.gift_info.img_basic = f'https://i0.hdslb.com/{index}.png'
    packet = {'cmd': 'SEND_GIFT_V2', 'data': {'pb': base64.b64encode(message.SerializeToString()).decode()}}
    at = datetime.fromtimestamp(start.timestamp()+1, timezone.utc).isoformat()
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE timeline (id INTEGER, room_id TEXT, kind TEXT, at_utc TEXT, source_key TEXT, data TEXT)')
        db.execute('CREATE TABLE raw_events (id INTEGER, room_id TEXT, command TEXT, received_utc TEXT, data TEXT)')
        db.execute('INSERT INTO raw_events VALUES (?,?,?,?,?)', (1,'123456','SEND_GIFT_V2',at,json.dumps(packet)))
        for index, item in enumerate(gift_items(packet['data'])):
            value = gift_message({'cmd':'SEND_GIFT','data':item})
            value['gift']['event_type'] = 'SEND_GIFT_V2'
            value['gift']['batch_index'] = index
            db.execute('INSERT INTO timeline VALUES (?,?,?,?,?,?)',
                       (index+1,'123456','gift',at,value['source_key'],json.dumps(value)))
    base = tmp_path/'base.mp4'
    run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=64x64:r=30:d=4',
         '-f','lavfi','-i','sine=frequency=440:duration=4','-c:v','libx264','-c:a','aac','-shortest',str(base)])
    (session/'config.json').write_text(json.dumps({'database':str(database),'room_id':'123456','start_utc':start.isoformat()}))
    (session/'offline/manifest.json').write_text(json.dumps({'cutoff':start.timestamp()+4,'rows':[
        {'path':str(base),'wall_start':start.timestamp(),'duration_sec':4,
         'size':base.stat().st_size,'mtime_ns':base.stat().st_mtime_ns}]}))
    cfg = dynamic_config()
    cfg['src'][0].update(srcType='img',srcTag=BLIND_TAG)
    catalog = tmp_path/'catalog.json'
    catalog.write_text(json.dumps({'gifts':[{'id':789,'name':'测试盲盒','effectId':777}],
                                   'effects':[{'effectId':777,'webMP4':'https://i0.hdslb.com/test.mp4',
                                               'webMP4Json':'https://i0.hdslb.com/test.json'}]}))
    monkeypatch.setattr(gift_assets, 'download', lambda url,limit:
                        json.dumps(cfg).encode() if url.endswith('.json') else packed.read_bytes())
    monkeypatch.setattr(gift_blind, 'download', lambda url,limit:png((0,255,0,255)))
    background = tmp_path/'background.png'
    background.write_bytes(png((255,200,0,255)))
    digits = tmp_path/'digits'
    digits.mkdir()
    for n in range(10):
        (digits/f'{n}.png').write_bytes(png((255,255,255,255)))
    cache = tmp_path/'cache'
    gift_blind.fetch_event(cache,session,1,catalog=catalog,card_background=background,card_digits=digits)
    return Namespace(session=session, cache=cache, catalog=catalog, font=None, before=.2, after=.2,
                     canvas='128x96',gap=0,event_aspect=[],event_region=[]), database


def test_fifty_results_render_one_opening_with_three_entries(blind_case, tmp_path):
    args, _ = blind_case
    binding = gift_blind.bind_event(args.session,2)
    assert binding['event_ids'] == [1,2,3] and sum(i['num'] for i in binding['results']) == 50
    plan = gift_batch.make_batch(args)
    assert len(plan['scenes']) == 1 and len(plan['scenes'][0]['overlays']) == 1
    assert plan['scenes'][0]['overlays'][0]['event_ids'] == [1,2,3]
    result = gift_batch.render_batch(plan,tmp_path/'output')
    assert result['verified'] and result['rendered_events'] == 3 and result['uncovered_events'] == 0
    assert len(list((tmp_path/'output/scene-0000/assets').glob('result-card-*.png'))) == 1
    edited = copy.deepcopy(plan)
    edited['scenes'][0]['overlays'][0]['event_ids'] = [1]
    with pytest.raises(ValueError,match='批次的结果覆盖'):
        gift_batch.validate_batch(edited)


@pytest.mark.parametrize('change',['missing','count','time','sender','raw','snapshot'])
def test_blind_binding_rejects_missing_or_conflicting_results(blind_case,change):
    args,database = blind_case
    with sqlite3.connect(database) as db:
        if change == 'missing':
            db.execute('DELETE FROM timeline WHERE id=3')
        elif change in ('raw','snapshot'):
            packet = json.loads(db.execute('SELECT data FROM raw_events').fetchone()[0])
            msg = Gift.FromString(base64.b64decode(packet['data']['pb']))
            if change == 'raw':
                msg.items[0].num = 99
            else:
                msg.items[0].ClearField('gift_info')
            packet['data']['pb'] = base64.b64encode(msg.SerializeToString()).decode()
            db.execute('UPDATE raw_events SET data=?',(json.dumps(packet),))
        elif change == 'time':
            db.execute("UPDATE timeline SET at_utc='2030-01-01T00:00:02+00:00' WHERE id=3")
        else:
            value = json.loads(db.execute('SELECT data FROM timeline WHERE id=3').fetchone()[0])
            value['gift']['num' if change=='count' else 'sender_uid'] = 99
            db.execute('UPDATE timeline SET data=? WHERE id=3',(json.dumps(value),))
    with pytest.raises(ValueError,match='盲盒'):
        gift_blind.bind_event(args.session,1)


def test_resources_are_frozen_and_render_does_not_download(blind_case,monkeypatch):
    args,_ = blind_case
    plan = gift_batch.make_batch(args)
    monkeypatch.setattr(gift_blind,'download',lambda *a:pytest.fail('render must remain offline'))
    gift_batch.validate_batch(plan)
    resource = plan['scenes'][0]['dynamic']['resources']['icons'][0]
    from pathlib import Path
    Path(resource['path']).write_bytes(png((255,0,0,255)))
    with pytest.raises(ValueError,match='图片指纹'):
        gift_batch.validate_batch(plan)


def test_single_blind_event_uses_same_formal_render_entry(blind_case,tmp_path):
    args,database = blind_case
    with sqlite3.connect(database) as db:
        packet = json.loads(db.execute('SELECT data FROM raw_events').fetchone()[0])
        msg = Gift.FromString(base64.b64decode(packet['data']['pb']))
        del msg.items[1:]
        msg.items[0].num = 1
        packet['data']['pb'] = base64.b64encode(msg.SerializeToString()).decode()
        db.execute('UPDATE raw_events SET data=?',(json.dumps(packet),))
        db.execute('DELETE FROM timeline WHERE id>1')
        value = json.loads(db.execute('SELECT data FROM timeline WHERE id=1').fetchone()[0])
        value['gift']['num'] = 1
        db.execute('UPDATE timeline SET data=?',(json.dumps(value),))
    # 将已验证的样式重新绑定新的一盒报文，不复用旧批次资源绑定。
    old = next((args.cache/'blind-resources').glob('*.json'))
    resources = json.loads(old.read_text())
    resources['binding'] = gift_blind.bind_event(args.session,1)
    resources['icons'] = resources['icons'][:1]
    gift_assets.write_json(gift_blind.resource_index(args.cache,resources['binding']),resources)
    single = Namespace(**vars(args), asset=None, base=None, at=None, event_id=1,
                       room=None,gift_id=None,count=1,username=None,base_display_aspect=None,
                       region=None,placement='center-fit')
    plan = gift_render.make_plan(single)
    assert plan['dynamic']['resources']['binding']['results'][0]['num'] == 1
    assert gift_render.render(plan,tmp_path/'single')['verified']
