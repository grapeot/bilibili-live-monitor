"""SEND_GIFT_V2 protobuf已知字段的最小描述；未知字段由protobuf运行时跳过。

字段号参考公开协议描述：
https://github.com/Akegarasu/blivedm-go/blob/master/pb/events.proto
这里只描述读取所需字段，不生成/维护整套站点protobuf代码。
"""
import base64

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

MAX_PROTO_BYTES = 4 * 1024 * 1024


def gift_class():
    schema = descriptor_pb2.FileDescriptorProto(name='live_gift.proto', package='livegift', syntax='proto3')

    def message(name, fields):
        result = schema.message_type.add(name=name)
        for number, field_name, kind in fields:
            field = result.field.add(name=field_name, number=number, label=1)
            if kind.endswith('?'):
                kind = kind[:-1]
                result.oneof_decl.add(name='_' + field_name)
                field.proto3_optional = True
                field.oneof_index = len(result.oneof_decl) - 1
            if kind == 'uint':
                field.type = 4  # uint64，保留完整UID而非截断到32位。
            elif kind == 'str':
                field.type = 9
            else:
                field.type = 11
                field.type_name = '.livegift.' + kind.rstrip('[]')
                field.label = 3 if kind.endswith('[]') else 1

    message('Base', [(1, 'name', 'str')])
    message('User', [(1, 'uid', 'uint'), (2, 'base', 'Base')])
    message('BlindGift', [(1, 'blind_gift_config_id', 'uint'), (2, 'original_gift_id', 'uint'),
                          (3, 'original_gift_name', 'str'), (6, 'original_gift_price', 'uint?')])
    message('Item', [(1, 'gift_id', 'uint'), (2, 'gift_name', 'str'), (3, 'num', 'uint'),
                     (5, 'price', 'uint?'), (7, 'total_coin', 'uint?'), (8, 'coin_type', 'str'),
                     (9, 'tid', 'str'), (10, 'timestamp', 'uint')])
    message('Gift', [(1, 'uid', 'uint'), (2, 'uname', 'str'), (9, 'blind_gift', 'BlindGift'),
                     (10, 'items', 'Item[]'), (15, 'sender', 'User')])
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName('livegift.Gift'))


Gift = gift_class()


def gift_items(data):
    encoded = data['pb']
    if not isinstance(encoded, str) or len(encoded) > MAX_PROTO_BYTES:
        raise ValueError('礼物protobuf超过大小限制或不是base64字符串')
    event = Gift.FromString(base64.b64decode(encoded, validate=True))
    if not event.items:
        raise ValueError('礼物protobuf没有结果')
    uid = event.sender.uid or event.uid
    author = event.sender.base.name or event.uname
    blind = ({'original_gift_id': event.blind_gift.original_gift_id,
              'original_gift_name': event.blind_gift.original_gift_name,
              'original_gift_price': (event.blind_gift.original_gift_price
                                      if event.blind_gift.HasField('original_gift_price') else None)}
             if event.HasField('blind_gift') else None)
    for item in event.items:
        yield {'uid': uid, 'uname': author, 'giftId': item.gift_id, 'giftName': item.gift_name,
               'num': item.num or 1, 'price': item.price if item.HasField('price') else None,
               'total_coin': item.total_coin if item.HasField('total_coin') else None,
               'coin_type': item.coin_type or None, 'tid': item.tid,
               'timestamp': item.timestamp, 'blind_gift': blind}
