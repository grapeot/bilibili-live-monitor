"""显式下载带校验的Silero v5.1.2 ONNX模型，不在服务启动时访问网络。"""
import argparse
import hashlib
import os
import tempfile
import urllib.request
from pathlib import Path

URL = 'https://raw.githubusercontent.com/snakers4/silero-vad/v5.1.2/src/silero_vad/data/silero_vad.onnx'
SHA256 = '2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f'


def prepare(path):
    path = Path(path).expanduser()
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == SHA256:
        return path
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(8 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError('VAD模型SHA256不匹配，未安装')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=os.getenv('VAD_MODEL_PATH', '.local/models/silero_vad.onnx'))
    print(prepare(parser.parse_args().output))
