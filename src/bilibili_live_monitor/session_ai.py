"""通过已安装的OpenCode客户端管理本场专属会话，不触碰共享server。"""
import json
import subprocess

BRIDGE = r'''
import json, sys
from opencode_skill.client import OpenCodeClient
c = OpenCodeClient()
v = json.loads(sys.argv[1])
def call(method, path, **kwargs):
    r = c._session.request(method, c.base_url + path, headers=c.headers, timeout=20, **kwargs)
    r.raise_for_status()
    return r.json() if r.content else None
operation = v['operation']
if operation == 'preflight':
    from opencode_skill.jobs import submit_dry_run
    class FinalTextClient:
        def __getattr__(self, key):
            return getattr(c, key)
        def get_session_messages(self, session_id):
            messages = c.get_session_messages(session_id)
            return [{**m, 'parts': [p for p in m.get('parts', []) if p.get('type') == 'text']}
                    for m in messages]
    # SDK旧版将reasoning也拼入答案；保持其原有预检流程，只核对实际text部分。
    checked = submit_dry_run(FinalTextClient(), title='Live monitor preflight', model=v['model'],
                            agent='build', send_timeout=10, wait_max_seconds=120)
    result = {'verification': checked.verification, 'status': checked.status}
elif operation == 'create':
    project = call('GET', '/project/current', params={'directory': v['project']})
    if project.get('worktree') != v['project']:
        raise RuntimeError('OpenCode worktree mismatch')
    sessions = call('GET', '/session', params={'directory': v['project']})
    found = [s for s in sessions if s.get('title') == v['title'] and s.get('directory') == v['project']]
    s = found[0] if found else call('POST', '/session', params={'directory': v['project']}, json={'title': v['title']})
    s = call('GET', '/session/' + s['id'])
    if s.get('directory') != v['project'] or s.get('projectID') != project['id']:
        raise RuntimeError('OpenCode project mismatch')
    result = {'id': s['id']}
elif operation in ('status', 'abort'):
    params = {'directory': v['project']} if v.get('project') else {}
    s = call('GET', '/session/' + v['session'], params=params)
    directory = s['directory']
    if v.get('project') and directory != v['project']:
        raise RuntimeError('OpenCode session directory mismatch')
    if operation == 'status':
        result = call('GET', '/session/status', params={'directory': directory}).get(v['session'], {'type': 'idle'})
    else:
        result = call('POST', '/session/' + v['session'] + '/abort', params={'directory': directory})
print(json.dumps(result))
'''


def ai_control(config, operation, **kwargs):
    result = subprocess.run([config['opencode_python'], '-c', BRIDGE,
        json.dumps({'operation': operation, 'project': config.get('opencode_project'), **kwargs})], cwd=config['opencode_cwd'],
        capture_output=True, text=True, check=True, timeout=180 if operation == 'preflight' else 90)
    return json.loads(result.stdout)
