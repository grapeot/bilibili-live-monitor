"""校验可信本地配置；热加载房间及未来场次的发布、模型路由。"""
from copy import deepcopy

from .browser import room_url


def validate_rooms(rooms):
    if not isinstance(rooms, dict):
        raise ValueError('rooms必须是对象')
    for rid, room in rooms.items():
        if not isinstance(rid, str) or room_url(rid).rsplit('/', 1)[-1] != rid:
            raise ValueError('自动化房间必须使用规范房间ID')
        if not isinstance(room, dict) or any(
                not isinstance(room.get(key), str) or not room[key].strip() for key in ('label', 'intro')):
            raise ValueError('自动化房间必须有label和intro')
        if type(room.get('welcome_enabled', False)) is not bool:
            raise ValueError('welcome_enabled必须是布尔值')
        if room.get('content_mode', 'music') not in ('music', 'talk'):
            raise ValueError('content_mode必须是music或talk')
        if 'record_mode' in room and room['record_mode'] not in ('none', 'audio', 'video'):
            raise ValueError('record_mode必须是none、audio或video')
        if 'local_only' in room and type(room['local_only']) is not bool:
            raise ValueError('local_only必须是布尔值')
        if 'notification_command' in room and (not isinstance(room['notification_command'], list)
                or any(not isinstance(arg, str) or not arg.strip() for arg in room['notification_command'])):
            raise ValueError('notification_command必须是命令字符串数组')
        greeting = room.get('greeting', '欢迎 {names} {suffix}')
        if not isinstance(greeting, str) or '{names}' not in greeting:
            raise ValueError('欢迎模板必须包含{names}')
        try:
            greeting.format(names='访客', suffix='来到直播间～')
        except (KeyError, ValueError, IndexError, AttributeError) as error:
            raise ValueError('欢迎模板只支持{names}和{suffix}') from error
        if not isinstance(room.get('followup_rules', {}), dict):
            raise ValueError('followup_rules必须是对象')


def reload_plan(previous, incoming):
    """拒绝删除和启动参数变化，防止部分配置生效或意外停止旧场。"""
    if not isinstance(incoming, dict):
        raise ValueError('自动化配置必须是对象')
    incoming = deepcopy(incoming)
    rooms = incoming.get('rooms', {})
    validate_rooms(rooms)
    if set(previous.get('rooms', {})) - set(rooms):
        raise ValueError('热加载不删除房间；停止守候请修改platform policy')
    def fixed(config):
        result = deepcopy(config)
        result.pop('rooms', None)
        workflow = result.get('session_workflow', {})
        if not isinstance(workflow, dict):
            raise ValueError('session_workflow必须是对象')
        for key in ('publish', 'model', 'final_model', 'final_subagent', 'final_concurrency'):
            workflow.pop(key, None)
        return result
    if fixed(previous) != fixed(incoming):
        raise ValueError('热加载只支持rooms及未来场次的publish/model/final_model/final_subagent/final_concurrency；'
                         '其他设置需要重启')
    from .session_workflow import validate_settings
    workflow = incoming.get('session_workflow', {})
    publish = workflow.get('publish', {})
    if not isinstance(publish, dict):
        raise ValueError('publish必须是对象')
    targets = publish.get('rooms', {})
    default = publish.get('default', {})
    if not isinstance(targets, dict) or not isinstance(default, dict):
        raise ValueError('publish.rooms和publish.default必须是对象')
    for target in [default, *targets.values()]:
        if not isinstance(target, dict) or any(
                not isinstance(target.get(key, ''), str) for key in ('url', 'remote')):
            raise ValueError('发布目标必须是对象，url和remote必须是字符串')
    validate_settings(incoming.get('session_workflow', {}), rooms)
    return incoming


def capture_overrides(rooms):
    return {rid: {'record_mode': cfg['record_mode']} for rid, cfg in rooms.items() if 'record_mode' in cfg}
