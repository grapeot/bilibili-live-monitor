"""INTERACT_WORD_V2 的已知 protobuf 字段；未知字段由运行时跳过。

字段号参考 https://github.com/Akegarasu/blivedm-go/blob/master/pb/events.proto
"""
import base64

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory


def interaction_class():
    schema = descriptor_pb2.FileDescriptorProto(name='live_interaction.proto', package='interaction', syntax='proto3')
    for name, fields in (
        ('Base', [(1, 'name', 9), (4, 'is_mystery', 8)]),
        ('User', [(1, 'uid', 4), (2, 'base', 'Base')]),
        ('Interaction', [(1, 'uid', 4), (2, 'username', 9), (5, 'msg_type', 13),
                         (7, 'timestamp', 13), (8, 'timestamp_ms', 4), (15, 'trigger_time', 4),
                         (16, 'guard_type', 13), (22, 'uinfo', 'User')]),
    ):
        message = schema.message_type.add(name=name)
        for number, field_name, kind in fields:
            field = message.field.add(name=field_name, number=number, label=1)
            field.type = 11 if isinstance(kind, str) else kind
            if isinstance(kind, str):
                field.type_name = '.interaction.' + kind
    pool = descriptor_pool.DescriptorPool()
    pool.Add(schema)
    return message_factory.GetMessageClass(pool.FindMessageTypeByName('interaction.Interaction'))


Interaction = interaction_class()


def interaction_data(data):
    encoded = data['pb']
    if not isinstance(encoded, str) or len(encoded) > 4 * 1024 * 1024:
        raise ValueError('互动protobuf超过大小限制或不是base64字符串')
    event = Interaction.FromString(base64.b64decode(encoded, validate=True))
    return {'uid': event.uinfo.uid or event.uid,
            'uname': event.uinfo.base.name or event.username,
            'is_mystery': event.uinfo.base.is_mystery,
            'msg_type': event.msg_type, 'timestamp': event.timestamp,
            'trigger_time': event.trigger_time, 'guard_level': event.guard_type}
