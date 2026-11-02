import json
import subprocess

import pytest
from test_activity import trend

from bilibili_live_monitor.activity_page import SCRIPT
from bilibili_live_monitor.song_annotations import load_songs, verify_songs
from bilibili_live_monitor.trends_render import render


def sample():
    windows = [{'source_key': 'a', 'start': 0, 'end': 10, 'label': 'singing', 'music_source': 'probable_live'},
               {'source_key': 'b', 'start': 20, 'end': 30, 'label': 'singing', 'music_source': 'probable_live'}]
    bands = [{'start': 0, 'end': 10, 'label': 'singing'},
             {'start': 10, 'end': 20, 'label': 'speaking'}, {'start': 20, 'end': 30, 'label': 'singing'}]
    annotations = {'schema_version': 1, 'dataset_id': 'synthetic', 'songs': [
        {'title': '测试歌曲', 'confidence': 'high', 'source_keys': ['a', 'b']}]}
    return windows, bands, annotations


def test_songs_never_bridge_speech_and_do_not_publish_source_keys():
    windows, bands, annotations = sample()
    songs = load_songs(annotations, windows, bands, 'synthetic')
    assert songs == [{'id': 'song-1', 'title': '测试歌曲', 'segments': [
        {'start': 0, 'end': 10}, {'start': 20, 'end': 30}]}]
    verify_songs(songs, bands)
    songs[0]['segments'] = [{'start': 0, 'end': 30}]
    with pytest.raises(ValueError, match='超出'):
        verify_songs(songs, bands)


def test_songs_reject_wrong_dataset_and_non_live_windows():
    windows, bands, annotations = sample()
    with pytest.raises(ValueError, match='数据集'):
        load_songs(annotations, windows, bands, 'other')
    windows[0]['music_source'] = 'recording'
    with pytest.raises(ValueError, match='现场'):
        load_songs(annotations, windows, bands, 'synthetic')


def test_low_confidence_titles_are_omitted():
    windows, bands, annotations = sample()
    annotations['songs'][0]['confidence'] = 'low'
    assert load_songs(annotations, windows, bands, 'synthetic') == []


def test_overlapping_song_names_are_rejected():
    songs = [{'id': name, 'title': name, 'segments': [{'start': 0, 'end': 10}]}
             for name in ('first', 'second')]
    with pytest.raises(ValueError, match='重叠'):
        verify_songs(songs, [{'start': 0, 'end': 10, 'label': 'singing'}])


def test_song_title_is_escaped_in_tooltip_and_list():
    data = trend()
    start = 1893456000
    songs = [{'id': 'song-1', 'title': '<script>bad</script>',
              'segments': [{'start': start, 'end': start+10}]}]
    page = render(data, songs=songs)
    assert '<script>bad</script>' not in page
    assert page.count('&lt;script&gt;bad&lt;/script&gt;') >= 5
    assert page.count('class="song-hit"') == 4
    assert 'class="song-chip"' in page


def test_editing_one_window_clips_song_overlay_instead_of_hiding_whole_song():
    start = 1893456000
    data = {**trend(), 'songs': [{'id': 'song-1', 'title': '合成歌曲',
                                'segments': [{'start': start, 'end': start+20}]}],
            'windows': [{'start': start+i*10, 'end': start+(i+1)*10, 'label': 'singing',
                         'music_source': 'probable_live', 'text': '', 'reason': '', 'provenance': 'manual'}
                        for i in range(2)]}
    harness = r'''
const vm=require('node:vm'),input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements={},layer={innerHTML:''},songLayer={innerHTML:''},span={textContent:''};
const button={dataset:{songId:'song-1'},disabled:false,querySelector(){return span;}};
const document={getElementById(id){return elements[id]??={value:'all',textContent:'',innerHTML:'',handlers:{},addEventListener(k,f){this.handlers[k]=f;}};},
 querySelectorAll(s){return s==='[data-song-layer]'?[songLayer]:s==='.song-chip'?[button]:[layer];},dispatchEvent(){}};
document.getElementById('activity-data').textContent=JSON.stringify(input.state);
vm.runInNewContext(input.script,{document,Date,Event:class{}});
const edit=(i,value)=>document.getElementById('review-rows').handlers.change({target:{dataset:{i:String(i)},value}});
edit(0,'speaking');const partial={html:songLayer.innerHTML,disabled:button.disabled,time:span.textContent};
edit(1,'speaking');const empty={html:songLayer.innerHTML,disabled:button.disabled,time:span.textContent};
edit(0,'singing');console.log(JSON.stringify({partial,empty,restored:{html:songLayer.innerHTML,disabled:button.disabled}}));
'''
    result = subprocess.run(['node', '-e', harness], input=json.dumps({'state': data, 'script': SCRIPT}),
                            text=True, capture_output=True, check=True)
    output = json.loads(result.stdout)
    assert f'data-start="{start+10}" data-end="{start+20}"' in output['partial']['html']
    assert not output['partial']['disabled']
    assert output['empty'] == {'html': '', 'disabled': True, 'time': '当前无有效唱段'}
    assert f'data-start="{start}" data-end="{start+10}"' in output['restored']['html']
    assert not output['restored']['disabled']
