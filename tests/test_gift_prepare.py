"""真实参数头恢复复用已有合成码流fixture，不修改生产cache策略。"""
from test_media_codec import capture as capture
from test_media_codec import source as source

from bilibili_live_monitor import media_codec as codec
from bilibili_live_monitor.gift_media import _expanded_matroska, inspect_clip, prepare_base


def test_raw_prepare_only_writes_explicit_private_output(source, tmp_path):
    original_sha = codec._sha(source)
    before = set(source.parent.iterdir())
    # Sibling output is outside the source media directory.
    out = tmp_path.parent / (tmp_path.name + '-gift-prepare')
    result = prepare_base(source, out)
    assert result['header_patched']
    assert not result['full_file_decode_verified']
    assert set(source.parent.iterdir()) == before
    assert not (source.parent / '.codec-media').exists()
    assert codec._sha(source) == original_sha
    assert codec._packets(source) == codec._packets(result['prepared'])
    clip = inspect_clip(result['prepared'], .1, .5)
    assert clip['decoded_size'] == [160, 128]


def test_larger_codec_private_rebuilds_lengths_and_preserves_packets(source, tmp_path):
    report = codec.inspect_video(source)
    # Extra trailing AVCC extension space exercises growth beyond the old allocation.
    initial = bytes.fromhex(report['initial_avcc']) + b'\x00' * 16
    before = codec._sha(source)
    output = tmp_path / 'expanded.mkv'
    with source.open('rb') as src, output.open('xb') as dst:
        _expanded_matroska(src, dst, bytes.fromhex(report['header_avcc']), initial)
    assert codec._packets(source) == codec._packets(output)
    assert codec._decode(output)
    assert codec._sha(source) == before
    assert inspect_clip(output, .1, .5)['decoded_size'] == [160, 128]
