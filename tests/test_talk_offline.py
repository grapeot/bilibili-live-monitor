import json

from bilibili_live_monitor.live_share import epoch
from bilibili_live_monitor.live_share_control import read_session_source
from bilibili_live_monitor.session_offline import recording_files


def test_recording_discovery_accepts_wav_and_scopes_session(tmp_path):
    for name in ('abcd-20300101T000010Z-ef12-00000.wav', 'abcd-20300101T000010Z-ef12-00001.wav',
                 'abcd-20291231T230000Z-ef12-00000.wav', 'abcd-20300101T020000Z-ef12-00000.wav', 'unrelated.wav'):
        (tmp_path/name).touch()
    config={'recording_root':str(tmp_path),'start_utc':'2030-01-01T00:00:00Z'}
    groups=recording_files(config,epoch('2030-01-01T01:00:00Z'))
    assert len(groups)==1
    assert sorted(index for entries in groups.values() for index, _ in entries)==[0,1]


def test_talk_offline_keeps_unrecorded_live_windows(tmp_path, monkeypatch):
    from bilibili_live_monitor import live_share_control as control
    before={'seq':1,'start_utc':'2030-01-01T00:00:00Z','end_utc':'2030-01-01T00:00:30Z'}
    replaced={'seq':2,'start_utc':'2030-01-01T00:01:00Z','end_utc':'2030-01-01T00:01:30Z'}
    after={'seq':3,'start_utc':'2030-01-01T00:02:00Z','end_utc':'2030-01-01T00:02:30Z'}
    offline={**replaced,'seq':-1}
    path=tmp_path/'offline_segments.json'
    path.write_text(json.dumps([offline]))
    monkeypatch.setattr(control,'read_source',lambda *a: {'segments':[before,replaced,after]})
    result=read_session_source({'database':'unused','room_id':'1','start_utc':before['start_utc'],
                                'workspace':str(tmp_path),'offline_segments':str(path),'content_mode':'talk'})
    assert [s['seq'] for s in result['segments']]==[1,-1,3]
