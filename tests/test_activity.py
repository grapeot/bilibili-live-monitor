import argparse
import hashlib
import json
import shutil
import subprocess
import wave

import pytest

from bilibili_live_monitor.activity import (
    apply_labels,
    bands,
    configure_parser,
    dataset_id,
    encode_audio,
    load_windows,
    main,
    suggest,
    text_features,
    verify,
)
from bilibili_live_monitor.activity_page import SCRIPT
from bilibili_live_monitor.trends import aggregate


def trend():
    result = {'schema_version': 1, 'room_id': '123456', 'title': '合成测试',
              'since': '2030-01-01T00:00:00+00:00', 'until': '2030-01-01T00:05:00+00:00',
              'timezone': 'Asia/Shanghai', 'bin_seconds': 300, 'gap_seconds': 60,
              'source': {'entries': [], 'gifts': [], 'stocks': [], 'raw_times': []}}
    result['summary'] = aggregate(result)
    return result


def record(**fields):
    return {'source_key': 'clip:0', 'room_id': '123456', 'start_utc': '2030-01-01T00:00:00Z',
            'end_utc': '2030-01-01T00:00:30Z', 'relative_start': 0, 'relative_end': 30,
            'sha256': 'a'*64, 'source_path': '/synthetic/audio.mkv',
            'text': '欢迎大家，谢谢你的礼物，接下来给你们唱一首歌。', **fields}


def test_candidates_are_conservative_for_short_and_quiet_audio():
    assert suggest('你好')[0] == 'unknown'
    assert suggest('欢迎大家，我们今天继续聊天，欢迎来到这里。', -65)[0] == 'unknown'
    assert suggest(record()['text'])[0] == 'speaking'
    assert suggest('为什么天上的星星会微笑，为什么远方的山谷有回响，我走过云朵画下的街角，把没有名字的风装进行囊。')[0] == 'singing'


def test_mixed_is_not_forced_to_a_song():
    text = '谢谢大家送来的礼物。欢迎大家来到今天的直播间。' + '月光洒在遥远的海面上，你的心从来不曾遗忘。'*3
    assert suggest(text)[0] == 'mixed'


def test_brief_thanks_does_not_dominate_long_lyrics():
    text = '谢谢。' + '月光洒在遥远的海面上，你的心从来不曾遗忘。'*4
    assert suggest(text)[0] == 'singing'


def test_density_uses_actual_duration_and_is_not_a_classifier_alone():
    assert text_features('你好，世界。', 2)['chars_per_second'] == 2
    assert text_features('', 0)['chars_per_second'] == 0
    text = '月光洒在遥远的海面上，你的心从来不曾遗忘。'*4
    assert suggest(text, duration=10)[0] == suggest(text, duration=60)[0] == 'singing'
    assert text_features('谢谢礼物。月光洒在海上。', 30)['interaction_keyword_count'] == 1


def test_bands_preserve_gaps_overlap_and_partial_range():
    result = bands([{'start': -10, 'end': 20, 'label': 'singing', 'music_source': 'probable_live'},
                    {'start': 10, 'end': 30, 'label': 'speaking'}], 0, 40)
    assert [(b['start'], b['end'], b['label']) for b in result] == [
        (0, 10, 'singing'), (10, 20, 'unknown'), (20, 30, 'speaking'), (30, 40, 'unknown')]
    assert result[1]['reason'] == '重叠窗口分类不同'
    assert result[3]['reason'] == '无ASR覆盖'


def test_manual_revision_roundtrip(tmp_path):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record()))
    windows = load_windows(path, trend())
    apply_labels(windows, {'schema_version': 1, 'dataset_id': dataset_id(windows), 'labels': [
        {'source_key': 'clip:0', 'label': 'mixed', 'provenance': 'manual'}]})
    assert windows[0]['label'] == 'mixed' and windows[0]['provenance'] == 'manual'


def test_legacy_manual_song_does_not_imply_live_source(tmp_path):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record()))
    windows = load_windows(path, trend())
    apply_labels(windows, {'schema_version': 1, 'dataset_id': dataset_id(windows), 'labels': [
        {'source_key': 'clip:0', 'label': 'singing', 'provenance': 'manual'}]})
    assert windows[0]['provenance'] == 'manual'
    assert windows[0]['music_source'] == 'uncertain'
    assert all(b['label'] != 'singing' for b in bands(windows, windows[0]['start'], windows[0]['end']))


@pytest.mark.parametrize('source,actions,expected', [
    ('recording', [('i', 'singing')], 'recording'),
    ('uncertain', [('i', 'singing')], 'uncertain'),
    ('none', [('i', 'singing')], 'uncertain'),
    ('none', [('sourceI', 'recording'), ('i', 'singing')], 'recording'),
    ('none', [('sourceI', 'probable_live'), ('i', 'singing')], 'probable_live'),
    ('recording', [('i', 'speaking'), ('i', 'singing')], 'recording'),
])
def test_browser_activity_edit_preserves_source(tmp_path, source, actions, expected):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record()))
    windows = load_windows(path, trend())
    windows[0].update(label='mixed', music_source=source, provenance='semantic_review')
    state = {**trend(), 'windows': windows, 'dataset_id': dataset_id(windows)}
    harness = r'''
const vm=require('node:vm');
const input=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const elements={};const layer={innerHTML:''};let blob;
const document={getElementById(id){return elements[id]??=(
 {value:'all',textContent:'',innerHTML:'',handlers:{},addEventListener(k,f){this.handlers[k]=f;}});},
 querySelectorAll(selector){return selector==='.song-hit'?[]:[layer];},createElement(){return {click(){}};}};
document.getElementById('activity-data').textContent=JSON.stringify(input.state);
const context={document,console,Date,URL:{createObjectURL(b){blob=b;return 'blob:test';},revokeObjectURL(){}},
 Blob:class{constructor(parts){this.text=parts.join('');}},setTimeout(){}};
vm.runInNewContext(input.script,context);
for(const [key,value] of input.actions){document.getElementById('review-rows').handlers.change({target:{dataset:{[key]:'0'},value}});}
document.getElementById('export-labels').onclick();
console.log(JSON.stringify({exported:JSON.parse(blob.text),green:layer.innerHTML.includes('#22c55e')}));
'''
    result = subprocess.run(['node', '-e', harness], input=json.dumps(
        {'state': state, 'script': SCRIPT, 'actions': actions}), text=True, capture_output=True, check=True)
    observed = json.loads(result.stdout)
    assert observed['exported']['labels'][0]['music_source'] == expected
    assert observed['green'] == (expected == 'probable_live')
    apply_labels(windows, observed['exported'])
    assert windows[0]['label'] == 'singing' and windows[0]['music_source'] == expected


@pytest.mark.parametrize('changes', [dict(room_id='654321'), dict(relative_end=10),
                                    dict(relative_start=-1), dict(relative_end=float('nan')),
                                    dict(end_utc='2029-01-01T00:00:00Z'), dict(source_key='')])
def test_invalid_asr_cannot_be_painted(tmp_path, changes):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record(**changes)))
    with pytest.raises(ValueError):
        load_windows(path, trend())


@pytest.mark.parametrize('key,label,provenance', [('other', 'singing', 'manual'),
                                               ('clip:0', 'bad', 'manual'),
                                               ('clip:0', 'singing', 'model_truth')])
def test_invalid_revision_rejected(tmp_path, key, label, provenance):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record()))
    windows = load_windows(path, trend())
    with pytest.raises(ValueError):
        apply_labels(windows, {'schema_version': 1, 'dataset_id': dataset_id(windows), 'labels': [
            {'source_key': key, 'label': label, 'provenance': provenance}]})


def test_empty_coverage_is_explicit_gray():
    assert bands([], 0, 60) == [{'start': 0, 'end': 60, 'label': 'unknown', 'reason': '无ASR覆盖'}]


def test_cli_build_verify_and_markup_safety(tmp_path):
    data, asr, output = tmp_path/'data.json', tmp_path/'asr.jsonl', tmp_path/'report'
    data.write_text(json.dumps(trend()))
    asr.write_text(json.dumps(record(text='</script><script>alert(1)</script>')))
    parser = argparse.ArgumentParser()
    configure_parser(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['activity', 'build', '--trends', str(data), '--asr', str(asr),
                              '--output', str(output)])
    assert main(args) == 0
    assert verify(output)['windows'] == 1
    page = (output/'index.html').read_text()
    assert '</script><script>alert(1)</script>' not in page
    assert 'data-activity-layer' in page and '导出修订' in page
    assert main(args) == 2  # 不覆盖已有审阅版本。
    (output/'index.html').write_text('tampered')
    with pytest.raises(ValueError):
        verify(output)


def test_whole_other_window_excluded(tmp_path):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record(start_utc='2029-01-01T00:00:00Z',end_utc='2029-01-01T00:00:30Z')))
    assert load_windows(path, trend()) == []


def test_same_source_cannot_drift_on_wallclock(tmp_path):
    path = tmp_path/'asr.jsonl'
    path.write_text('\n'.join(json.dumps(r) for r in [record(), record(source_key='clip:1',
        start_utc='2030-01-01T00:01:00Z', end_utc='2030-01-01T00:01:30Z')]))
    with pytest.raises(ValueError, match='墙钟'):
        load_windows(path, trend())


def test_revision_binds_source_even_if_key_reused(tmp_path):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record()))
    windows = load_windows(path, trend())
    labels = {'schema_version': 1, 'dataset_id': dataset_id(windows), 'labels': [
        {'source_key': 'clip:0', 'label': 'singing', 'provenance': 'manual'}]}
    windows[0]['sha256'] = 'b'*64
    with pytest.raises(ValueError, match='数据集'):
        apply_labels(windows, labels)


def test_semantic_review_stays_candidate(tmp_path):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record()))
    windows = load_windows(path, trend())
    apply_labels(windows, {'schema_version': 1, 'dataset_id': dataset_id(windows), 'labels': [
        {'source_key': 'clip:0', 'label': 'singing', 'provenance': 'semantic_review'}]})
    assert windows[0]['provenance'] == 'semantic_review'
    assert windows[0]['music_source'] == 'uncertain'


def test_recording_and_unknown_source_are_not_live_singing_bands():
    result = bands([
        {'start': 0, 'end': 10, 'label': 'singing', 'music_source': 'recording'},
        {'start': 10, 'end': 20, 'label': 'singing', 'music_source': 'uncertain'},
        {'start': 20, 'end': 30, 'label': 'singing', 'music_source': 'probable_live'},
    ], 0, 40)
    assert [(b['start'], b['end']) for b in result if b['label'] == 'singing'] == [(20, 30)]
    assert result[-1]['reason'] == '无ASR覆盖'


def test_source_revision_preserves_playback_and_rejects_invalid_source(tmp_path):
    path = tmp_path/'asr.jsonl'
    path.write_text(json.dumps(record()))
    windows = load_windows(path, trend())
    override = {'schema_version': 1, 'dataset_id': dataset_id(windows), 'labels': [
        {'source_key': 'clip:0', 'label': 'singing', 'music_source': 'recording',
         'provenance': 'semantic_review', 'reason': '后文明确说明放的是录音'}]}
    apply_labels(windows, override)
    assert windows[0]['music_source'] == 'recording'
    assert not any(b['label'] == 'singing' for b in bands(windows, windows[0]['start'], windows[0]['end']))
    override['labels'][0]['music_source'] = 'none'
    with pytest.raises(ValueError, match='声源'):
        apply_labels(windows, override)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='需要ffmpeg')
def test_audio_offsets_must_fit_encoded_source(tmp_path):
    source = tmp_path/'one.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x00'*16000)
    before = source.read_bytes()
    windows = [{'sha256': hashlib.sha256(before).hexdigest(), 'source_path': str(source),
                'source_key': 'short', 'relative_start': 100, 'relative_end': 130}]
    with pytest.raises(ValueError, match='超出'):
        encode_audio(windows, tmp_path)
    assert source.read_bytes() == before
