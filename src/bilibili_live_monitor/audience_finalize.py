"""场次结束后的观众状态更新；独立总会话用指定子代理处理按人分片。"""
import json
import shlex
import sys
from pathlib import Path

from . import audience_crm as crm
from .live_share import atomic_json


def load_settings(session):
    path = Path(session['audience_crm_config'])
    config = json.loads(path.read_text())
    if config['room_id'] != session['room_id']:
        raise ValueError('本场CRM配置属于其他主播')
    agent = config.get('author_agent', 'grok' if config.get('agentic_sources') else 'ollama_glm_5_3_flash')
    if not isinstance(agent, str) or not agent.strip():
        raise ValueError('author_agent须为非空agent名称')
    config['author_agent'] = agent
    model = config.get('coordinator_model')
    if model is not None and (not isinstance(model, str) or '/' not in model or not model.strip()):
        raise ValueError('coordinator_model须为provider/model')
    return config, path


def advance(session, state):
    """返回True才允许本场收尾；失败不改旧档案，不重复提交不确定的任务。"""
    if not session.get('audience_crm_config'):
        return True
    from .session_ai import ai_control
    from .session_finalize import dispatch
    from .session_runner import ensure_ai, stop_ai
    config, path = load_settings(session)
    if config.get('coordinator_model'):
        session = {**session, 'model': config['coordinator_model']}
    state_path = Path(session.get('audience_state_path') or Path(session['workspace']) / 'workflow.json')
    root = Path(session['workspace']) / 'audience_finalize'
    root.mkdir(exist_ok=True)
    marker = root / 'plan.json'
    if not marker.exists():
        config['_include_session'] = session['_config_path']
        directory, plan = crm.prepare(config, crm.load_material(config))
        atomic_json(marker, {'run': str(directory), 'plan_id': plan['id']})
    saved = json.loads(marker.read_text())
    directory = Path(saved['run'])
    accepted = directory / 'accepted.json'
    if accepted.exists():
        current = json.loads((Path(config['workspace']) / 'current.json').read_text())
        if current['room_id'] != session['room_id']:
            raise ValueError('CRM接纳版本房间错误')
        if not stop_ai(session, state.get('audience_session'), children=True):
            return False
        if config.get('publish_on_finalize') and not session.get('local_only', False):
            from .audience_publish import publish
            result = publish(config)
        else:
            from .audience_pages import build
            result = build(config)
        state['audience_crm'] = {'plan_id': result['plan_id'], 'people': result['people'],
                                 'published': result.get('status') == 'published'}
        return True
    if not state.get('audience_session'):
        state['audience_session'] = ensure_ai(session, 'audience_finalize')
        atomic_json(state_path, state)
    ai_session = state['audience_session']
    tag = 'AUDIENCE_PLAN:' + saved['plan_id']
    if state.get('audience_dispatch_uncertain'):
        if ai_control(session, 'submitted', session=ai_session, marker=tag):
            state.pop('audience_dispatch_uncertain', None)
            state['audience_submitted'] = True
        else:
            raise ValueError('CRM提交结果不确定，须核查原会话，不自动重发')
    if not state.get('audience_submitted'):
        command = shlex.join([sys.executable, '-m', 'bilibili_live_monitor.audience_crm',
                             'finish', '--config', str(path), '--run', str(directory)])
        prompt = f'''{tag}
你负责此场CRM总协调，不改代码、不发布、不通知。读取{directory / 'plan.json'}及{directory / 'instructions.txt'}。
plan.json中reused=true的任务已由程序校验复用上一版result，直接保留无需重跑；仅对reused=false（本场有新互动、新提名或来源变更）的任务使用task的{config['author_agent']}子代理处理input/result，最多同时10个；不可静默改模型或创建下一级代理。
每个待更新任务读取自己的input与其中source_manifest指向的完整本房间来源，使用OpenCode工具自主查证后续；在保留previous有效历史信息的基础上增量合入新场变化，只写对应result，不改来源。没有source_manifest时仅使用原有input证据。
不能把JSON通过当作查证完成。对reused=false的任务须实际查证且校验通过；重点追查“未见回应/待核实”的点歌及首次入团送歌兑现情况，不把可从源文件查到的工作转交主播。
总任务重点抽查未完成承诺、已跨场兑现及同名歧义：一条引用真实不等于结论成立；必须核对双方上下文，不能挪用其他观众的回应。
全部完成后统一运行：{command}。finish失败须修复原结果，不伪造空结果通过，不修改计划或输入。
成功后报告完成数与未决问题。若指定子代理不可用，报告阻塞，不fallback。不能访问凭据、控制采集、发送直播消息或排定时任务。
'''

        def handoff():
            state['audience_dispatch_uncertain'] = True
            atomic_json(state_path, state)

        dispatch(session, ai_session, prompt, root, handoff)
        state.pop('audience_dispatch_uncertain', None)
        state['audience_submitted'] = True
    return False
