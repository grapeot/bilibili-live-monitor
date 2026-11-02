import copy
import hashlib
import json
import shutil

import pytest

from bilibili_live_monitor import trailer


@pytest.fixture
def plan(tmp_path, monkeypatch):
    source = tmp_path / 'source.mkv'
    source.write_bytes(b'synthetic video identity')
    monkeypatch.setattr(trailer, 'probe', lambda _: {
        'format': {'duration': '300'}, 'streams': [
            {'codec_type': 'video', 'width': 1920, 'height': 1080}, {'codec_type': 'audio'}]})
    monkeypatch.setattr(trailer, 'coverage', lambda *_: {'video': [(0, 300)], 'audio': [(0, 300)]})
    return {'schema_version': 1, 'subjects': {'demo': {'title': '合成直播', 'clips': [{
        'source': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'start': 10, 'end': 140, 'crop': [200, 100, 800, 600], 'label': '合成事件'}]}}}


def test_validate_binds_source_and_preserves_nonchronological_edit(plan):
    original = copy.deepcopy(plan)
    clips = plan['subjects']['demo']['clips']
    clips[0]['start'], clips[0]['end'] = 200, 270
    clips.append({**clips[0], 'start': 10, 'end': 70})
    selected = trailer.validate(plan, 'demo')
    assert selected['duration'] == 130
    assert [c['start'] for c in selected['clips']] == [200, 10]
    assert 'masks' not in original['subjects']['demo']['clips'][0]
    clips[0]['sha256'] = 'wrong'
    with pytest.raises(ValueError, match='指纹'):
        trailer.validate(plan, 'demo')


@pytest.mark.parametrize('field,value', [('start', float('nan')), ('end', float('inf')),
                                       ('start', True), ('start', -1), ('end', 301)])
def test_reject_unsafe_time(plan, field, value):
    plan['subjects']['demo']['clips'][0][field] = value
    with pytest.raises(ValueError):
        trailer.validate(plan, 'demo')


@pytest.mark.parametrize('crop', [[-1, 0, 800, 600], [1200, 0, 800, 600], [0, 0, 0, 600],
                                  [0., 0, 800, 600], [0, 0, 800]])
def test_reject_out_of_frame_crop(plan, crop):
    plan['subjects']['demo']['clips'][0]['crop'] = crop
    with pytest.raises(ValueError, match='矩形'):
        trailer.validate(plan, 'demo')


def test_title_is_not_interpolated_into_filter_program(plan):
    plan['subjects']['demo']['clips'][0]['label'] = "合成: '测试', [text]; %{}"
    clip = trailer.validate(plan, 'demo')['clips'][0]
    filters = trailer.video_filter(clip)
    assert clip['label'] not in filters
    assert 'drawtext' not in filters


def test_render_rejects_bad_plan_before_creating_output(plan, tmp_path):
    plan['subjects']['demo']['clips'][0]['end'] = 20
    output = tmp_path / 'result'
    with pytest.raises(ValueError, match='120至180'):
        trailer.render(plan, 'demo', tmp_path / 'font.ttf', output)
    assert not output.exists()


@pytest.mark.parametrize('kind', ['video', 'audio'])
def test_coverage_rejects_internal_holes(kind):
    spans = {'video': [(0, 30)], 'audio': [(0, 30)]}
    spans[kind] = [(0, 10), (11, 30)]
    with pytest.raises(ValueError, match='覆盖缺口'):
        trailer.require_coverage(spans, 0, 30)


def test_loudness_json_followed_by_ffmpeg_progress():
    result = trailer.parse_loudness('log line\n{"input_i": "-16.0", "input_tp": "-2.0"}\nsize=4KiB time=4.0\n')
    assert result['input_i'] == '-16.0'
    with pytest.raises(ValueError, match='未取得'):
        trailer.parse_loudness('no measurement')


def test_dynamic_normalization_is_explicit_not_silent():
    trailer.check_normalization('linear')
    trailer.check_normalization('dynamic', True)
    with pytest.raises(ValueError, match='显式接受'):
        trailer.check_normalization('dynamic')
    with pytest.raises(ValueError):
        trailer.check_normalization('unknown', True)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='真实媒体回归需要ffmpeg')
@pytest.mark.parametrize('video_duration,audio_duration', [(.1, 1), (1, .1)])
def test_real_asymmetric_tracks_rejected(tmp_path, video_duration, audio_duration):
    source = tmp_path / 'mismatch.mp4'
    trailer.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                 f'color=size=32x32:rate=30:duration={video_duration}', '-f', 'lavfi', '-i',
                 f'sine=frequency=440:sample_rate=48000:duration={audio_duration}',
                 '-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', str(source)])
    with pytest.raises(ValueError):
        trailer.require_coverage(trailer.coverage(source, trailer.probe(source)), 0, 1)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='真实媒体回归需要ffmpeg')
def test_real_short_video_long_audio_cannot_verify(tmp_path):
    movie = tmp_path / 'trailer.mp4'
    trailer.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                 'color=size=1280x720:rate=30:duration=0.1', '-f', 'lavfi', '-i',
                 'sine=frequency=440:sample_rate=48000:duration=120',
                 '-c:v', 'libx264', '-threads', '2', '-c:a', 'aac', str(movie)])
    (tmp_path / 'manifest.json').write_text(json.dumps({'status': 'complete', 'duration': 120,
                                                      'sha256': trailer.digest(movie), 'clips': [{}]}))
    with pytest.raises(ValueError, match='video'):
        trailer.verify(tmp_path)


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='真实媒体回归需要ffmpeg')
def test_real_rotation_rejected_before_cropping(tmp_path):
    source, rotated = tmp_path / 'raw.mp4', tmp_path / 'rotated.mp4'
    trailer.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                 'color=size=160x90:rate=1:duration=1', '-f', 'lavfi', '-i',
                 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-threads', '2',
                 '-c:a', 'aac', str(source)])
    trailer.run(['ffmpeg', '-v', 'error', '-display_rotation', '90', '-i', str(source),
                 '-c', 'copy', str(rotated)])
    plan = {'schema_version': 1, 'subjects': {'demo': {'clips': [{
        'source': str(rotated), 'sha256': trailer.digest(rotated), 'start': 0, 'end': 120,
        'crop': [0, 0, 80, 80], 'label': '合成'}]}}}
    with pytest.raises(ValueError, match='显示变换矩阵'):
        trailer.validate(plan, 'demo')
