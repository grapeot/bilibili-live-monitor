"""离线自动推进预算；与已经启动的单片执行时限独立。"""
import json
import time
from pathlib import Path


class BudgetPaused(RuntimeError):
    pass


def check_budget(config, state=None, *, now=None, kind=None):
    if state is None:
        path = Path(config.get('audience_state_path') or Path(config['workspace']) / 'workflow.json')
        if kind == 'audience_finalize' and not config.get('audience_state_path'):
            marker = Path(config['workspace']) / 'audience_finalize' / 'plan.json'
            if marker.exists():
                budget_path = json.loads(marker.read_text()).get('budget_state_path')
                if budget_path:
                    path = Path(budget_path)
        state = json.loads(path.read_text()) if path.exists() else {}
    if state.get('automation_paused') or (time.time() if now is None else now) >= state.get(
            'finish_deadline', float('inf')):
        raise BudgetPaused('离线收尾自动推进预算已用尽，保留现场等待人工处理')
