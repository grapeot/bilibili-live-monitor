"""按文件清单发布观众站；读回校验后原子切换随机路径的符号链接。"""
import hashlib
import json
import re
import secrets
import shlex
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .audience_pages import build
from .live_share import atomic_json


def target(config):
    remote = config['remote']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*:/[A-Za-z0-9_./-]+/', remote):
        raise ValueError('CRM远端路径格式无效')
    host, path = remote.split(':', 1)
    parsed = PurePosixPath(path)
    if '..' in parsed.parts or len(parsed.parts) < 4:
        raise ValueError('CRM远端路径不能包含上级目录或位于站点根')
    url, home = urlsplit(config['url']), urlsplit(config['home_url'])
    if (url.scheme != 'https' or url.netloc != home.netloc or url.username or url.password
            or url.query or url.fragment or not url.path.startswith(home.path.rstrip('/') + '/')
            or url.path == home.path or not path.endswith(url.path)):
        raise ValueError('CRM目标必须位于本主播主页内，且与远端路径一致')
    return host, parsed


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=180)


REMOTE = '''import os,sys
from pathlib import Path
phase,dest,version,pending=sys.argv[1:]
dest,version,pending=Path(dest),Path(version),Path(pending)
root=dest.parent
versions=root/('.'+dest.name+'-versions')
if root.resolve(strict=True)!=root or versions.resolve()!=versions:
    raise ValueError('CRM remote path escapes through symlink')
if version.parent!=versions or pending.parent!=root:
    raise ValueError('CRM remote transaction outside authorized root')
if os.path.lexists(dest) and (not dest.is_symlink() or dest.resolve(strict=True).parent!=versions):
    raise ValueError('CRM destination is not an owned version link')
if phase=='prepare':
    versions.mkdir(exist_ok=True)
    version.mkdir()
elif phase=='switch':
    if version.resolve(strict=True)!=version or not version.is_dir():
        raise ValueError('CRM version directory is not canonical')
    pending.symlink_to(version, target_is_directory=True)
    os.replace(pending,dest)
    if dest.resolve(strict=True)!=version:
        raise ValueError('CRM remote pointer mismatch')
else:
    raise ValueError('unknown CRM remote phase')
'''


def publish(config):
    import fcntl
    root = Path(config['workspace']).resolve()
    with (root / '.publish.lock').open('a') as lock:
        # 同一份主播配置只有一个发布者；忙时由外层重试，不排队发布旧版本。
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _publish(config)


def _publish(config):
    import fcntl
    host, destination = target(config)
    result = build(config)
    plan = result['plan_id']
    if not re.fullmatch('[a-f0-9]{64}', plan):
        raise ValueError('CRM发布版本ID无效')
    files = result['files']
    if any(not re.fullmatch(r'(?:index\.(?:html|json)|[a-f0-9]{32}/index\.html)', name) for name in files):
        raise ValueError('CRM发布清单越界')
    # 本地版本目录不可变；即使另一个构建切换output指针，本次仍读同一版。
    source = Path(result['directory'])
    hashes = {name: sha(source / name) for name in files}
    versions = destination.parent / ('.' + destination.name + '-versions')
    nonce = secrets.token_hex(8)
    version = versions / (plan + '-' + nonce)
    pending = destination.with_name(destination.name + '.next-' + nonce)

    def remote(phase):
        return ['ssh', host, shlex.join(['python3', '-c', REMOTE, phase, str(destination), str(version), str(pending)])]

    # 随机且必须新建的版本目录不接受已有目录/祖先symlink；不向旧版写任何文件。
    command(remote('prepare'))
    with tempfile.TemporaryDirectory(prefix='crm-publish-', dir=Path(config['workspace'])) as temporary:
        temp = Path(temporary)
        allowlist = temp / 'files.txt'
        allowlist.write_text('\n'.join(files) + '\n')
        command(['rsync', '-ac', '--files-from=' + str(allowlist), str(source) + '/', f'{host}:{version}/'])
        checked = temp / 'checked'
        checked.mkdir()
        command(['rsync', '-ac', '--files-from=' + str(allowlist), f'{host}:{version}/', str(checked) + '/'])
        if any(sha(checked / name) != value for name, value in hashes.items()):
            raise ValueError('远端CRM文件摘要与本次构建不一致，未切换站点')
    # 只替换本功能管理的符号链接，不能覆盖既有实体目录或其他站点。
    root = Path(config['workspace']).resolve()
    # 与finish共用短锁：切换前拒绝已被新current取代的构建，切换时也不能换current。
    with (root / '.lock').open('a') as state_lock:
        fcntl.flock(state_lock, fcntl.LOCK_EX)
        if json.loads((root / 'current.json').read_text())['plan_id'] != plan:
            raise ValueError('已有较新档案，拒绝发布旧版本')
        command(remote('switch'))
        receipt = {'plan_id': plan, 'scope': config['scope'], 'url': config['url'],
                   'remote': config['remote'], 'remote_version': str(version), 'files': hashes, 'status': 'published'}
        atomic_json(root / 'published.json', receipt)
    return {'plan_id': plan, 'scope': config['scope'], 'url': config['url'], 'files': len(files),
            'people': result.get('people', 0), 'status': 'published'}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(publish(json.loads(args.config.read_text())), ensure_ascii=False))


if __name__ == '__main__':
    main()
