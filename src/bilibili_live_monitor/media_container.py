"""独立MKV副本的有界Tracks重建；媒体Cluster不重封装、不重编码。"""


def expanded_matroska(src, dst, old, initial):
    from .media_codec import _elements

    def size_bytes(value):
        width = next(n for n in range(1, 9) if value < (1 << (7 * n)) - 1)
        return ((1 << (7 * width)) | value).to_bytes(width, 'big')

    def header():
        first = src.read(1)
        if not first:
            return None
        if not first[0]:
            raise ValueError('EBML元素ID无效')
        width = 9 - first[0].bit_length()
        if width > 4:
            raise ValueError('EBML元素ID超限')
        ident = first + src.read(width - 1)
        first = src.read(1)
        if not first or not first[0]:
            raise ValueError('EBML长度缺失')
        count = 9 - first[0].bit_length()
        raw = first + src.read(count - 1)
        if len(raw) != count or len(ident) != width:
            raise ValueError('EBML头不完整')
        size = int.from_bytes(raw, 'big') & ((1 << (7 * count)) - 1)
        return ident, size, count, ident + raw

    def copy_bytes(count, write=True):
        while count:
            block = src.read(min(count, 1024 * 1024))
            if not block:
                raise ValueError('Matroska元素提前结束')
            if write:
                dst.write(block)
            count -= len(block)

    matches = 0

    def tracks(data):
        nonlocal matches
        result = bytearray()
        for begin, ident, payload, end in _elements(data, 0, len(data)):
            if end > len(data):
                raise ValueError('Tracks元素越界')
            body = data[payload:end]
            if ident == 0xbf:
                continue
            if ident == 0xae:
                body = tracks(body)
            elif ident == 0x63a2 and body == old:
                body = initial
                matches += 1
            identity = ident.to_bytes((ident.bit_length() + 7) // 8, 'big')
            result.extend(identity + size_bytes(len(body)) + body)
        return bytes(result)

    item = header()
    if item is None or item[0] != bytes.fromhex('1a45dfa3'):
        raise ValueError('需要标准Matroska EBML头')
    dst.write(item[3])
    copy_bytes(item[1])
    item = header()
    if item is None or item[0] != bytes.fromhex('18538067'):
        raise ValueError('缺少Matroska Segment')
    dst.write(item[0] + b'\x01' + b'\xff' * 7)
    while (item := header()) is not None:
        ident, size, width, raw = item
        if size == (1 << (7 * width)) - 1:
            raise ValueError('不支持未知长度的Segment子元素')
        if ident in (bytes.fromhex('114d9b74'), bytes.fromhex('1c53bb6b'), b'\xbf'):
            copy_bytes(size, write=False)
        elif ident == bytes.fromhex('1654ae6b'):
            if size > 1024 * 1024:
                raise ValueError('Tracks头超出1MiB上限')
            body = src.read(size)
            if len(body) != size:
                raise ValueError('Tracks头不完整')
            body = tracks(body)
            dst.write(ident + size_bytes(len(body)) + body)
        else:
            dst.write(raw)
            copy_bytes(size)
    if matches != 1:
        raise ValueError('不能唯一定位需要替换的CodecPrivate')
