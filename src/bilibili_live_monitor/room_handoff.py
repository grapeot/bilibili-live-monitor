"""下播后交接已预留的固定来源；不更新整份policy、不改原单场任务。"""
import argparse
import fcntl
import json
import time
from pathlib import Path
from urllib.request import Request, urlopen

from .automation import confirmed_state
from .browser import room_url
from .live_share import atomic_json


def request(url, data=None, method=None):
    req = Request(url, data=None if data is None else json.dumps(data).encode(),
                  headers={'Content-Type': 'application/json'}, method=method)
    with urlopen(req, timeout=25) as response:
        return json.load(response)


def matching_fixed(platform, rid):
    """通过已观测规范ID匹配全部短号；任何未解析的其他固定来源均阻断交接。"""
    candidates = {c['url']: c for c in platform['candidates']}
    matches = []
    for value in platform['policy']['fixed_rooms']:
        url = room_url(value)
        candidate = candidates.get(url)
        if candidate is None:
            raise ValueError('固定来源缺少候选记录')
        canonical = candidate.get('room_id')
        if not canonical and url == room_url(rid):
            canonical = rid  # 目标规范ID由绑定的原场次配置提供。
        if not canonical:
            raise ValueError('固定来源尚未解析规范身份')
        if str(canonical) == rid:
            matches.append(candidate)
    return matches


def quiescent(platform, rid):
    # API只给全局启动计数，宁可等其他房间导航结束，也不漏掉本房未完成launch。
    return (platform['launching'] == 0 and platform['pending_pages'] == 0
            and not any(r['room_id'] == rid and r['status'] in ('capturing', 'reconnecting')
                        for r in platform['sources']))


def advance(config, state, *, api=request, persist=lambda value: None, execute=False):
    if state.get('phase') == 'completed':
        return state
    rid = config['room_id']
    source, destination = config['source_api'].rstrip('/'), config['destination_api'].rstrip('/')
    if source == destination:
        raise ValueError('交接需要两个独立实例')
    root = Path(config['session_directory'])
    local = json.loads((root / 'config.json').read_text())
    workflow = json.loads((root / 'workflow.json').read_text())
    identity = [rid, config['workflow_key'], config['start_utc']]
    if [local['room_id'], local['workflow_key'], local['start_utc']] != identity:
        raise ValueError('原场次身份已改变')
    if state.get('identity') not in (None, identity):
        raise ValueError('交接状态不属于本场次')
    state['identity'] = identity
    old, new = api(source + '/api/platform'), api(destination + '/api/platform')
    old_auto, new_auto = api(source + '/api/automation'), api(destination + '/api/automation')
    for instance in (old, new):
        policy = instance['policy']
        if (not policy['enabled'] or policy['mode'] != 'fixed'
                or policy['target_rooms'] != len(policy['fixed_rooms'])
                or policy['target_rooms'] > instance['capacity']):
            raise ValueError('需要全部启用且容量足够的预留固定名单')
    if new_auto['account']['mode'] != 'anonymous' or new_auto['account']['logged_in']:
        raise ValueError('目标实例不是已核验的未登录匿名实例')
    for auto in (old_auto, new_auto):
        room = next((r for r in auto['rooms'] if r['room_id'] == rid), None)
        if room is None or room['enabled']:
            raise ValueError('两端房间须预配置且欢迎均已关闭')
    originals, targets = matching_fixed(old, rid), matching_fixed(new, rid)
    if not originals or len(targets) != 1 or targets[0]['url'] != room_url(rid):
        raise ValueError('需要源规范映射与唯一的目标规范房间预留')
    intent = state.get('phase') in ('retiring_source', 'activating_destination')
    if not intent or not all(c['blocked'] for c in originals):
        parked = targets[0]
        if (not parked['blocked'] or parked.get('last_error') != 'operator_stopped'
                or new.get('status_refused')):
            raise ValueError('目标必须为本次预留的操作者停用状态，不解除平台拒绝')
        sealed = (workflow.get('stage') in ('sealing', 'offline_asr', 'offline_review', 'finalizing', 'completed')
                  and type(workflow.get('cutoff')) in (int, float))
        effective = [c for c in originals if not c.get('alias_duplicate')]
        offline = (effective and all(confirmed_state(c) == 'offline' and not c.get('blocked')
                   and not c.get('blocked_alias') for c in effective) and quiescent(old, rid))
        if not (sealed and offline):
            state.update(phase='waiting_boundary', checked_at=time.time(),
                         reason='等待新鲜两次下播、原场cutoff及无残留启动任务')
            if execute:
                persist(state)
            return state
        if not execute:
            return {**state, 'phase': 'ready', 'cutoff': workflow['cutoff']}
        state.update(phase='retiring_source', cutoff=workflow['cutoff'])
        persist(state)
    if not execute:
        return {**state, 'phase': 'ready_to_resume'}
    if not all(c['blocked'] for c in originals):
        # 现有服务原子验证revision并停止全部已解析别名；不调用configure，保留其他场次ASR。
        api(source + '/api/platform/decisions', {'revision': old['revision'],
            'stop': [c['url'] for c in originals], 'reason': '授权的下播跨实例交接：退休原来源'})
    released = api(source + '/api/platform')
    if not all(c['blocked'] for c in matching_fixed(released, rid)):
        raise RuntimeError('源来源停用尚未读回确认')
    if not quiescent(released, rid):
        state['phase'] = 'retiring_source'
        persist(state)
        return state
    new = api(destination + '/api/platform')
    targets = matching_fixed(new, rid)
    if len(targets) != 1 or targets[0]['url'] != room_url(rid) or new.get('status_refused'):
        raise ValueError('目标预留已改变或平台拒绝，停止交接')
    if targets[0]['blocked']:
        if targets[0].get('last_error') != 'operator_stopped':
            raise ValueError('目标已出现新的拒绝或停用原因，不自动解除')
        state['phase'] = 'activating_destination'
        persist(state)
        api(destination + '/api/platform/candidates/' + rid + '/unpark', {'revision': new['revision']})
    target = matching_fixed(api(destination + '/api/platform'), rid)[0]
    if target['blocked']:
        raise RuntimeError('目标启用尚未读回确认')
    state.update(phase='completed', completed_at=time.time())
    persist(state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    path = Path(config['state_path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads(path.read_text()) if path.exists() else {}
        result = advance(config, state, execute=args.execute, persist=lambda value: atomic_json(path, value))
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
