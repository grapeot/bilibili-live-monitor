"""直接拥有Chrome和服务进程组；绝对截止时间独立于Agent会话执行。"""
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def parse_deadline(value):
    deadline = datetime.fromisoformat(value)
    if deadline.tzinfo is None:
        raise ValueError('截止时间必须包含时区，例如2030-01-01T22:00:00-08:00')
    if deadline.timestamp() <= time.time():
        raise ValueError('截止时间已经过去，不启动测试进程')
    return deadline.timestamp()


def run_owned(commands, directory, deadline, env=None, stop_event=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stop_event = stop_event or threading.Event()
    children, logs = [], []
    grace = min(3, max(0, (deadline-time.time())/4))
    manifest = {'owner_pid': os.getpid(), 'until': datetime.fromtimestamp(deadline).astimezone().isoformat(),
                'deadline_epoch': deadline, 'status': 'starting', 'children': []}
    manifest_path = directory / 'runtime.json'

    def save():
        temporary = manifest_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        temporary.replace(manifest_path)

    lock = (directory / '.owner.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        for label, command in commands:
            if time.time() >= deadline:
                raise RuntimeError('截止时间已到')
            log = (directory / f'{label}.log').open('ab')
            logs.append(log)
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, env=env)
            children.append(child)
            manifest['children'].append({'label': label, 'pid': child.pid, 'pgid': child.pid})
            save()
        manifest['status'] = 'running'
        save()
        stopping = False
        while time.time() < deadline and not stop_event.is_set():
            if not stopping and deadline-time.time() <= grace:
                manifest['status'] = 'stopping'
                save()
                for child in reversed(children):
                    if child.poll() is None:
                        child.terminate()
                stopping = True
            if not stopping and any(child.poll() is not None for child in children):
                manifest['error'] = '拥有的进程提前退出，结束整个测试会话'
                break
            stop_event.wait(min(0.2, max(0, deadline-time.time())))
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in reversed(children):
            try:
                child.wait(timeout=max(0, min(3, deadline-time.time())))
            except subprocess.TimeoutExpired:
                pass
        # 根进程退出也不跳过其进程组，防止ASR或Chrome Helper残留。
        for child in reversed(children):
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        manifest['status'] = 'stopped'
        manifest['ended_at'] = datetime.now().astimezone().isoformat()
        manifest['reason'] = 'deadline' if time.time() >= deadline else 'stop_or_child_exit'
        save()
        for log in logs:
            log.close()
        lock.close()
    return manifest


def run_session(settings, until):
    deadline = parse_deadline(until)
    endpoint = urlparse(settings.cdp_url)
    if endpoint.hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('自管模式的CDP_URL必须指向本机回环地址')
    for host, port in ((endpoint.hostname, endpoint.port or 9222),
                       (os.getenv('APP_HOST', '127.0.0.1'), int(os.getenv('APP_PORT', '8765')))):
        try:
            connection = socket.create_connection((host, port), timeout=0.3)
        except OSError:
            continue
        connection.close()
        raise RuntimeError(f'端口已占用：{host}:{port}；不接管其他运行实例')
    chrome = os.getenv('CHROME_BIN') or (
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
        if sys.platform == 'darwin' else shutil.which('google-chrome'))
    if not chrome or not Path(chrome).exists():
        raise RuntimeError('找不到Chrome，请设置CHROME_BIN')
    profile = settings.data_dir / 'chrome_profile'
    profile.mkdir(parents=True, exist_ok=True)
    browser_command = [chrome, f'--remote-debugging-port={urlparse(settings.cdp_url).port or 9222}',
                       '--remote-debugging-address=127.0.0.1', f'--user-data-dir={profile}',
                       '--no-first-run', '--no-default-browser-check', '--disable-extensions',
                       '--disable-session-crashed-bubble', '--disable-background-timer-throttling',
                       '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
                       '--autoplay-policy=no-user-gesture-required', 'about:blank']
    event = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: event.set())
    run_owned([('chrome', browser_command), ('backend', [sys.executable, '-m', 'bilibili_live_monitor', 'serve'])],
              settings.data_dir, deadline, env={**os.environ, 'DATA_DIR': str(settings.data_dir)}, stop_event=event)
