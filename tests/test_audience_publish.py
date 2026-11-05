import json
from pathlib import Path

import pytest

from bilibili_live_monitor import audience_publish as module


def settings(tmp_path):
    return {'room_id': '10001', 'scope': 'example', 'workspace': str(tmp_path),
            'home_url': 'https://example.com/s/secret/', 'url': 'https://example.com/s/secret/audience-secret/',
            'remote': 'example:/var/www/example/s/secret/audience-secret/', 'output_dir': str(tmp_path / 'public')}


@pytest.mark.parametrize('remote', ['example:/', 'example:/var/www/../other/',
                                  'example:/var/www/other/', 'example:/var/www/a;touch /tmp/file/'])
def test_publish_target_rejects_scope_escape(tmp_path, remote):
    with pytest.raises(ValueError):
        module.target({**settings(tmp_path), 'remote': remote})


def test_publish_reads_back_before_switch_and_only_exports_allowlist(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    source = tmp_path / 'public'
    source.mkdir()
    for name in ['index.html', 'index.json']:
        (source / name).write_text('synthetic public content')
    (source / 'private.json').write_text('must not publish')
    (tmp_path / 'current.json').write_text(json.dumps({'plan_id': 'a' * 64}))
    monkeypatch.setattr(module, 'build', lambda _: {'plan_id': 'a' * 64, 'files': ['index.html', 'index.json'], 'directory': str(source)})
    calls = []

    def run(args):
        calls.append(args)
        if args[0] == 'rsync' and args[-1].endswith('/checked/'):
            check = Path(args[-1])
            for name in ['index.html', 'index.json']:
                (check / name).write_text((source / name).read_text())
        if args[0] == 'rsync':
            allow = Path(next(a.split('=', 1)[1] for a in args if a.startswith('--files-from=')))
            assert allow.read_text().splitlines() == ['index.html', 'index.json']

    monkeypatch.setattr(module, 'command', run)
    assert module.publish(cfg)['status'] == 'published'
    assert [c[0] for c in calls] == ['ssh', 'rsync', 'rsync', 'ssh']
    assert 'os.replace' in calls[-1][-1]
    receipt = json.loads((tmp_path / 'published.json').read_text())
    assert set(receipt['files']) == {'index.html', 'index.json'}


def test_failed_readback_does_not_switch(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    source = tmp_path / 'public'
    source.mkdir()
    (source / 'index.html').write_text('expected')
    monkeypatch.setattr(module, 'build', lambda _: {'plan_id': 'b' * 64, 'files': ['index.html'], 'directory': str(source)})
    calls = []

    def run(args):
        calls.append(args)
        if args[0] == 'rsync' and args[-1].endswith('/checked/'):
            (Path(args[-1]) / 'index.html').write_text('wrong')

    monkeypatch.setattr(module, 'command', run)
    with pytest.raises(ValueError, match='未切换'):
        module.publish(cfg)
    assert len(calls) == 3
    assert not (tmp_path / 'published.json').exists()


def test_remote_preflight_rejects_symlink_versions(tmp_path, monkeypatch):
    import shlex
    import subprocess
    import sys
    cfg = settings(tmp_path)
    destination = tmp_path / 'site/home/audience-secret'
    destination.parent.mkdir(parents=True)
    versions = destination.parent / ('.' + destination.name + '-versions')
    other = tmp_path / 'another-site'
    other.mkdir()
    versions.symlink_to(other, target_is_directory=True)
    monkeypatch.setattr(module, 'target', lambda _: ('synthetic', destination))
    source = tmp_path / 'public'
    source.mkdir()
    (source / 'index.html').write_text('allowed only here')
    monkeypatch.setattr(module, 'build', lambda _: {'plan_id': 'c' * 64, 'files': ['index.html'], 'directory': str(source)})
    calls = []

    def run(args):
        calls.append(args)
        parts = shlex.split(args[-1])
        return subprocess.run([sys.executable, *parts[1:]], check=True, capture_output=True)

    monkeypatch.setattr(module, 'command', run)
    with pytest.raises(subprocess.CalledProcessError):
        module.publish(cfg)
    assert len(calls) == 1
    assert not list(other.iterdir())


def test_stale_publish_and_concurrent_publisher_are_rejected(tmp_path, monkeypatch):
    cfg = settings(tmp_path)
    source = tmp_path / 'public'
    source.mkdir()
    (source / 'index.html').write_text('old')
    (tmp_path / 'current.json').write_text(json.dumps({'plan_id': 'a' * 64}))
    monkeypatch.setattr(module, 'build', lambda _: {'plan_id': 'a' * 64, 'files': ['index.html'], 'directory': str(source)})
    calls = []

    def run(args):
        calls.append(args)
        if len(calls) == 1:
            with pytest.raises(BlockingIOError):
                module.publish(cfg)
        if args[0] == 'rsync' and args[-1].endswith('/checked/'):
            (Path(args[-1]) / 'index.html').write_text('old')
            (tmp_path / 'current.json').write_text(json.dumps({'plan_id': 'b' * 64}))

    monkeypatch.setattr(module, 'command', run)
    with pytest.raises(ValueError, match='拒绝发布旧版本'):
        module.publish(cfg)
    assert len(calls) == 3
    assert not (tmp_path / 'published.json').exists()


def test_real_remote_protocol_ignores_unrelated_stale_next(tmp_path, monkeypatch):
    import shlex
    import shutil
    import subprocess
    import sys
    cfg = settings(tmp_path)
    destination = tmp_path / 'site/home/audience-secret'
    destination.parent.mkdir(parents=True)
    stale = destination.with_name(destination.name + '.next')
    stale.symlink_to(tmp_path / 'missing-old-transaction')
    monkeypatch.setattr(module, 'target', lambda _: ('synthetic', destination))
    source = tmp_path / 'public'
    source.mkdir()
    (source / 'index.html').write_text('the new version')
    (tmp_path / 'current.json').write_text(json.dumps({'plan_id': 'd' * 64}))
    monkeypatch.setattr(module, 'build', lambda _: {'plan_id': 'd' * 64, 'files': ['index.html'], 'directory': str(source)})

    def run(args):
        if args[0] == 'ssh':
            parts = shlex.split(args[-1])
            return subprocess.run([sys.executable, *parts[1:]], check=True, capture_output=True)
        src, dest = args[-2:]
        src = src.removeprefix('synthetic:')
        dest = dest.removeprefix('synthetic:')
        shutil.copyfile(Path(src) / 'index.html', Path(dest) / 'index.html')

    monkeypatch.setattr(module, 'command', run)
    assert module.publish(cfg)['status'] == 'published'
    assert destination.is_symlink() and (destination / 'index.html').read_text() == 'the new version'
    assert stale.is_symlink()  # 不删除未知事务，但它不再阻断本次提交
