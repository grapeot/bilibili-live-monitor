"""解码播放器已有WebSocket中的弹幕，不另建连接或使用付费接口。"""
import hashlib
import json
import struct
import zlib
from datetime import datetime, timezone

import brotli

MAX_PACKET_BYTES = 4 * 1024 * 1024


def packets(frame, depth=0):
    if not isinstance(frame, bytes) or len(frame) > MAX_PACKET_BYTES or depth > 3:
        return
    offset = 0
    while offset + 16 <= len(frame):
        size, header, version, operation, _ = struct.unpack_from('>IHHII', frame, offset)
        if not 16 <= header <= size or offset + size > len(frame):
            return
        payload = frame[offset + header:offset + size]
        offset += size
        if operation != 5:
            continue
        if version in (2, 3):
            decoder = zlib.decompressobj() if version == 2 else brotli.Decompressor()
            expanded = bytearray()
            for start in range(0, len(payload), 256):
                block = payload[start:start + 256]
                chunk = (decoder.decompress(block, MAX_PACKET_BYTES + 1 - len(expanded))
                         if version == 2 else decoder.process(block))
                expanded.extend(chunk)
                if len(expanded) > MAX_PACKET_BYTES or (version == 2 and decoder.unconsumed_tail):
                    raise ValueError('弹幕解压超过大小限制')
            yield from packets(bytes(expanded), depth + 1)
        elif version in (0, 1):
            try:
                yield json.loads(payload)
            except (ValueError, UnicodeError):
                continue


def messages(frame):
    result = []
    try:
        for packet in packets(frame):
            try:
                if not isinstance(packet, dict) or not str(packet.get('cmd', '')).startswith('DANMU_MSG'):
                    continue
                info = packet['info']
                text = info[1]
                if not isinstance(text, str) or not text.strip():
                    continue
                timestamp = float(info[0][4])
                if timestamp > 10**11:
                    timestamp /= 1000
                at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                key = hashlib.sha256(json.dumps(info, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                result.append({'text': text[:10000], 'author': str(info[2][1])[:200],
                               'at_utc': at, 'source_key': key})
            except (ValueError, TypeError, IndexError, KeyError, OverflowError, OSError):
                continue
    except (ValueError, TypeError, IndexError, KeyError, OverflowError, OSError, zlib.error, brotli.error):
        # 畸形帧不能影响采音；已成功解码的消息仍保留。
        pass
    return result
