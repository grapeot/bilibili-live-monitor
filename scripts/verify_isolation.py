"""在两个静音标签页播放不同合成音，验证采集没有混音；需要已启动的专用Chrome。"""
import asyncio
import base64
import io
import json
import wave

import numpy as np
from playwright.async_api import async_playwright

from bilibili_live_monitor.browser import MUTE_SCRIPT
from bilibili_live_monitor.capture import close_pcm, read_pcm, start_pcm
from bilibili_live_monitor.config import load_settings
from bilibili_live_monitor.monitor import validate_source


async def main():
    settings = load_settings()
    pages = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(settings.cdp_url)
        try:
            for frequency in (440, 880):
                page = await browser.contexts[0].new_page()
                pages.append(page)
                await page.add_init_script(MUTE_SCRIPT)
                await page.goto('http://127.0.0.1:8765', wait_until='domcontentloaded')
                await page.set_content('<video muted loop></video>')
                await page.evaluate(MUTE_SCRIPT)
                samples = (np.sin(2*np.pi*frequency*np.arange(16000*4)/16000)*12000).astype('<i2')
                buffer = io.BytesIO()
                with wave.open(buffer, 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(16000)
                    wav.writeframes(samples.tobytes())
                data = base64.b64encode(buffer.getvalue()).decode()
                await page.evaluate('''async data=>{const v=document.querySelector('video');
                    v.src='data:audio/wav;base64,'+data;v.muted=true;await v.play()}''', data)
                await start_pcm(page)
                await page.evaluate('document.querySelector("video").pause()')
                await page.wait_for_function('!document.querySelector("video").paused', timeout=5000)
            results = []
            buffers = await asyncio.gather(*(read_pcm(page, 2) for page in pages))
            for frequency, page, (audio, payload) in zip((440, 880), pages, buffers):
                pcm = np.frombuffer(audio, dtype='<i2')
                spectrum = abs(np.fft.rfft(pcm))
                peak = np.argmax(spectrum) * 16000 / len(pcm)
                other = 880 if frequency == 440 else 440
                bin_index = round(other * len(pcm) / 16000)
                leakage_db = 20*np.log10((spectrum[bin_index]+1)/(spectrum.max()+1))
                result = {'source_hz': frequency, 'captured_hz': round(peak, 1),
                          'other_source_db': round(float(leakage_db), 1), 'muted': payload['muted'],
                          'missing_samples': payload['missing_samples'],
                          'coverage_ratio': round(len(audio)/32000/((payload['ended']-payload['started'])/1000),3)}
                assert abs(peak-frequency) < 2 and leakage_db < -30 and payload['muted'], result
                assert payload['missing_samples'] < 1600
                results.append(result)
            # 原生enabled=false仍可提供零样本；必须拒绝而不是当作正常静音。
            probe = pages[0]
            await probe.evaluate('window.__pcmMonitor.tracks[0].enabled=false')
            _, disabled = await read_pcm(probe, 0.5)
            try:
                validate_source(disabled)
            except RuntimeError:
                assert not disabled['track_enabled']
            else:
                raise AssertionError('被禁用的源音轨错误地通过校验')
            await probe.evaluate('''()=>{const t=window.__pcmMonitor.tracks[0];
                t.enabled=true;t.dispatchEvent(new Event('mute'))}''')
            # 受控派发mute事件，核验浏览器端累计计时与Python拒绝逻辑。
            _, source_muted = await read_pcm(probe, 0.2)
            await probe.evaluate("window.__pcmMonitor.tracks[0].dispatchEvent(new Event('unmute'))")
            assert source_muted['track_muted_ms'] >= 100
            try:
                validate_source(source_muted)
            except RuntimeError:
                results.append({'disabled_source_rejected':True,
                                'controlled_mute_event_ms':round(source_muted['track_muted_ms'],1)})
            else:
                raise AssertionError('源mute事件错误地通过校验')
            for page in pages:
                await close_pcm(page)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            settings.data_dir.mkdir(exist_ok=True)
            (settings.data_dir / 'isolation.json').write_text(json.dumps(results, indent=2))
        finally:
            for page in pages:
                await page.close()


asyncio.run(main())
