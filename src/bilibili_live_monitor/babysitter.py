"""异步AI决策适配器：命令接收请求/结果文件路径，不能阻塞录制循环。"""
import asyncio
import json
import os
import signal
import uuid

from .platform import Decision
from .store import utc_now


async def decide(platform):
    directory = platform.settings.data_dir/'babysitter'
    directory.mkdir(exist_ok=True)
    attempt = uuid.uuid4().hex
    request, result = directory/f'{attempt}.request.json', directory/f'{attempt}.result.json'
    health = platform.summary()
    health['candidate_count'] = len(health['candidates'])
    health['candidates'] = sorted(health['candidates'], key=lambda c: -c['priority'])[:200]
    request.write_text(json.dumps({'health': health, 'decision_schema': Decision.model_json_schema()},
                                  ensure_ascii=False))
    process = None
    platform.last_ai = {'status': 'running', 'attempt': attempt, 'started_utc': utc_now()}
    platform.store.save_control('babysitter', platform.last_ai)
    try:
        with (directory/f'{attempt}.log').open('wb') as log:
            process = await asyncio.create_subprocess_exec(
                *platform.settings.babysitter_command, str(request), str(result),
                stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            await asyncio.wait_for(process.wait(), platform.settings.babysitter_timeout_seconds)
        if process.returncode:
            raise RuntimeError(f'AI命令退出码{process.returncode}')
        if result.stat().st_size > 65536:
            raise ValueError('AI决策超过64KiB')
        decision = Decision.model_validate_json(result.read_text())
        receipt = platform.apply_decision(decision)
        platform.last_ai.update(status='applied', receipt=receipt)
    except asyncio.CancelledError:
        platform.last_ai['status'] = 'cancelled'
        raise
    except Exception as error:
        platform.last_ai.update(status='failed', error=f'{type(error).__name__}: {error}')
    finally:
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        platform.last_ai['ended_utc'] = utc_now()
        platform.store.save_control('babysitter', platform.last_ai)
        platform.event('babysitter', **platform.last_ai)


async def babysit(platform):
    while True:
        if platform.policy.enabled:
            await decide(platform)
        await asyncio.sleep(platform.settings.babysitter_interval_seconds)
