"""独立登录资料的Chrome生命周期；只管理本服务创建的进程。"""
import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import psutil


class Accounts:
    def __init__(self, settings, config):
        self.settings = settings
        self.profiles = config.get('profiles', {})
        for profile in self.profiles.values():
            if profile.get('mode', 'account') not in ('account', 'anonymous'):
                raise ValueError('profile mode需为account或anonymous')
        self.path = settings.data_dir / 'active_account.json'
        saved = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.active = saved.get('active', config.get('active_profile'))
        if self.profiles and self.active not in self.profiles:
            raise ValueError('当前账号不在已配置profile中')
        self.process = None
        self.log = None
        self.epoch = 0
        self.switching = False
        self.identity = {'logged_in': False, 'uid': None, 'name': None}

    @property
    def anonymous(self):
        return self.profiles.get(self.active, {}).get('mode', 'account') == 'anonymous'

    def summary(self):
        return {'managed': bool(self.profiles), 'active': self.active,
                'mode': 'anonymous' if self.anonymous else 'account',
                'profiles': [{'id': key, 'label': value['label'], 'mode': value.get('mode', 'account')}
                             for key, value in self.profiles.items()],
                'switching': self.switching, **self.identity}

    async def start(self):
        if not self.profiles:
            return
        endpoint = urlparse(self.settings.cdp_url)
        if endpoint.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError('账号管理只允许本机CDP')
        with socket.socket() as probe:
            if probe.connect_ex((endpoint.hostname, endpoint.port or 9222)) == 0:
                raise RuntimeError('CDP端口已占用，拒绝接管其他Chrome')
        profile = Path(self.profiles[self.active]['path']).expanduser().resolve()
        profile.mkdir(parents=True, exist_ok=True)
        chrome = os.getenv('CHROME_BIN', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
        if not Path(chrome).is_file():
            raise RuntimeError('请通过CHROME_BIN指定Chrome')
        self.log = (self.settings.data_dir / 'account_chrome.log').open('ab')
        self.process = subprocess.Popen(
            [chrome, f'--user-data-dir={profile}', f'--remote-debugging-port={endpoint.port or 9222}',
             '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check',
             '--disable-extensions', '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
             '--disable-backgrounding-occluded-windows', '--autoplay-policy=no-user-gesture-required',
             '--mute-audio', 'about:blank'], stdout=self.log, stderr=self.log)
        self.path.write_text(json.dumps({'active': self.active}))
        self.epoch += 1
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError('账号Chrome启动失败')
            with socket.socket() as probe:
                if probe.connect_ex((endpoint.hostname, endpoint.port or 9222)) == 0:
                    if self.anonymous:
                        from .browser import Browser
                        browser = Browser(self.settings.cdp_url)
                        try:
                            await self.refresh(browser)
                        finally:
                            await browser.close()
                    return
            await asyncio.sleep(.1)
        raise RuntimeError('等待账号Chrome超时')

    async def refresh(self, browser):
        try:
            context = await browser.connect()
            response = await context.request.get('https://api.bilibili.com/x/web-interface/nav', timeout=10000)
            payload = await response.json()
            data = payload.get('data') or {}
            if self.anonymous:
                if (response.status != 200 or payload.get('code') not in (0, -101)
                        or not isinstance(data, dict) or data.get('isLogin') is not False):
                    raise RuntimeError('无法确认匿名登录态，或anonymous profile意外登录')
        except BaseException:
            if self.anonymous:
                await self.close()
            raise
        identity = {'logged_in': data.get('isLogin') is True,
                    'uid': str(data.get('mid')) if data.get('isLogin') else None,
                    'name': data.get('uname') if data.get('isLogin') else None}
        if self.identity.get('uid') != identity['uid']:
            self.epoch += 1
        self.identity = identity
        return self.summary()

    async def login(self, browser):
        if self.anonymous:
            raise ValueError('匿名profile不支持登录，请切换账号profile')
        context = await browser.connect()
        page = await context.new_page()
        await page.goto('https://passport.bilibili.com/login', wait_until='domcontentloaded')
        await page.bring_to_front()

    async def close(self):
        if self.process:
            descendants = []
            if self.process.poll() is None:
                try:
                    descendants = psutil.Process(self.process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    pass
                self.process.terminate()
            for child in descendants:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            try:
                await asyncio.to_thread(self.process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                pass
            if self.process.poll() is None:
                self.process.kill()
            for child in descendants:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            await asyncio.to_thread(self.process.wait)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None
        self.identity = {'logged_in': False, 'uid': None, 'name': None}
