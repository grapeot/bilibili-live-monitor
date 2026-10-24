import asyncio
import json
import os
from pathlib import Path


class ASR:
    def __init__(self, settings, worker_script=None):
        self.settings = settings
        self.process = None
        self._ready = False
        self.log = None
        self.lock = asyncio.Lock()
        self.restart_count = 0
        self.retry_count = 0
        self.timeout_count = 0
        self.last_error = None
        self.worker_script = worker_script or Path(__file__).with_name('asr_worker.py')

    @property
    def ready(self):
        return self._ready and self.process is not None and self.process.returncode is None

    async def start(self):
        async with self.lock:
            if self.ready:
                return
            try:
                await self._start()
            except BaseException:
                await self.close()
                raise

    async def _start(self):
        if self.log:
            self.log.close()
        self.log = (self.settings.data_dir / 'asr.stderr.log').open('ab')
        self.process = await asyncio.create_subprocess_exec(
            self.settings.asr_python, '-u', str(self.worker_script),
            env={**os.environ, 'ASR_MODEL': self.settings.asr_model},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log)
        response = await asyncio.wait_for(self.process.stdout.readline(), self.settings.asr_start_timeout)
        if not response or not json.loads(response).get('ready'):
            raise RuntimeError('ASR启动失败，查看本地 asr.stderr.log')
        self._ready = True
        self.last_error = None

    async def transcribe(self, path, req_id):
        for attempt in range(2):
            try:
                await self.start()
                request = {'req_id': req_id, 'wav_path': str(path)}
                self.process.stdin.write((json.dumps(request) + '\n').encode())
                await self.process.stdin.drain()
                try:
                    line = await asyncio.wait_for(self.process.stdout.readline(), self.settings.asr_infer_timeout)
                except TimeoutError:
                    self.timeout_count += 1
                    raise
                if not line:
                    raise RuntimeError(f'ASR子进程提前退出，exit={self.process.returncode}；查看asr.stderr.log')
                result = json.loads(line)
                if result.get('req_id') != req_id:
                    raise RuntimeError('ASR响应标识不匹配')
                if result.get('error'):
                    self.last_error = str(result['error'])
                    if attempt == 0:
                        self.retry_count += 1
                        continue  # 合法错误响应也重试同一WAV，保留已加载模型。
                else:
                    self.last_error = None
                return result
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception as error:
                self.last_error = f'{type(error).__name__}: {error}'
                await self.close()
                if attempt == 1:
                    raise
                self.restart_count += 1
                self.retry_count += 1

    async def close(self):
        self._ready = False
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.log:
            self.log.close()
            self.log = None
