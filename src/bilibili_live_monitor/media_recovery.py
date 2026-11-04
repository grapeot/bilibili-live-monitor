"""稳定尾片的有界无损恢复。只写副本，保留源文件及音频包身份。"""
import json
import subprocess
import time
from pathlib import Path

from . import offline_asr
from .live_share import atomic_json


def packet_hash(path):
    result = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path),
        '-map', '0:a:0', '-c', 'copy', '-f', 'hash', '-hash', 'sha256', '-'],
        check=True, capture_output=True, text=True, timeout=60)
    digest = result.stdout.strip()
    if not digest.startswith('SHA256=') or len(digest) != 71:
        raise RuntimeError('缺少音频包校验值')
    return digest


def recover(row, workspace):
    """仅用于单场稳定封口后、AI输入冻结前；非此类失败保持原错误。"""
    source = Path(row['path'])
    if (row.get('sealed') != 'unsealed_or_incomplete' or not row.get('audio_codec')
            or row.get('duration_sec') is not None or source.suffix not in ('.mkv', '.mka')):
        return row
    workspace = Path(workspace)
    if (workspace / 'finalize/plan.json').exists() or (workspace / 'talk_finalize/plan.json').exists():
        raise ValueError('AI输入已冻结，不能恢复或替换素材')
    offline_asr._verify_identity(row)
    if time.time_ns() - source.stat().st_mtime_ns < 300 * 10**9:
        raise RuntimeError('素材尚未稳定，暂不恢复')
    directory = workspace / 'offline/recovered'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (row['sha256'] + source.suffix)
    receipt = target.with_suffix('.json')
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        fixed = saved['row']
        if saved['original_sha256'] != row['sha256'] or Path(fixed['path']) != target:
            raise ValueError('恢复凭证与原素材不匹配')
        offline_asr._verify_identity(fixed)
        offline_asr._verify_identity(row)
        return {**row, **fixed}
    temporary = target.with_suffix('.tmp' + source.suffix)
    # 未完成的副本可以覆盖；原始源文件始终只读。
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
                    '-map', '0', '-c', 'copy', str(temporary)],
                   check=True, capture_output=True, timeout=60)
    fixed = offline_asr.probe(directory, temporary)
    if fixed['sealed'] != 'sealed_readable':
        raise RuntimeError('无损副本仍不可读取')
    original_packets = packet_hash(source)
    if original_packets != packet_hash(temporary):
        raise RuntimeError('恢复副本音频数据不一致')
    offline_asr._verify_identity(row)
    temporary.replace(target)
    fixed = offline_asr.probe(directory, target)
    fixed.update(room_id=row['room_id'], recovery_original_path=row['path'],
                 recovery_original_sha256=row['sha256'],
                 recovery_original_identity={k: row[k] for k in ('path', 'size', 'mtime_ns', 'sha256')},
                 recovery_method='lossless_remux_verified_audio_packets')
    atomic_json(receipt, {'original_sha256': row['sha256'], 'audio_packet_sha256': original_packets, 'row': fixed})
    return {**row, **fixed}
