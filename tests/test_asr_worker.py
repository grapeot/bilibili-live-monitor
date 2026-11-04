import json
import os
import subprocess
import sys
from pathlib import Path

import bilibili_live_monitor.asr_worker as worker


def test_direct_worker_does_not_shadow_stdlib_platform(tmp_path):
    (tmp_path / 'mlx_qwen3_asr.py').write_text('''import platform
from types import SimpleNamespace
assert isinstance(platform.system(), str)
def load_model(name):
    return object(), None
def transcribe(path, **kwargs):
    return SimpleNamespace(text='合成转写', truncated=False)
''')
    env = {**os.environ, 'PYTHONPATH': str(tmp_path)}
    env.pop('PYTHONSAFEPATH', None)
    result = subprocess.run([sys.executable, '-u', str(Path(worker.__file__).resolve())],
                            cwd=tmp_path, env=env, input='{"req_id":"test","wav_path":"unused"}\n',
                            text=True, capture_output=True, timeout=10, check=True)
    ready, transcript = [json.loads(line) for line in result.stdout.splitlines()]
    assert ready == {'ready': True}
    assert transcript['req_id'] == 'test' and transcript['text'] == '合成转写'
