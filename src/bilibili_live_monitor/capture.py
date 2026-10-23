"""逐播放器 PCM 采集；AudioWorklet 在音频线程收样，不依赖 WebM 分片时间戳。"""
import asyncio
import base64

WORKLET = '''
class MonitorTap extends AudioWorkletProcessor {
    constructor() {
        super(); this.buffer=new Float32Array(sampleRate); this.used=0; this.missing=0;
        this.port.onmessage=e=>{if(e.data.flush) this.flush(e.data.id)};
    }
    flush(id=null) {
        const data=this.buffer.slice(0,this.used);
        this.port.postMessage({samples:data.buffer,missing:this.missing,id},[data.buffer]);
        this.used=0;this.missing=0;
    }
    process(inputs,outputs) {
        const channels=inputs[0] || [], length=outputs[0][0].length;
        for(let i=0;i<length;i++) {
            let value=0;
            for(const channel of channels) value+=channel[i] || 0;
            this.buffer[this.used++]=channels.length ? value/channels.length : 0;
            if(!channels.length)this.missing++;
            if(this.used===this.buffer.length)this.flush();
        }
        return true;
    }
}
registerProcessor('monitor-tap',MonitorTap);
'''

START_PCM = '''async (worklet) => {
    if(window.__pcmMonitor) await window.__pcmMonitor.close();
    const video=document.querySelector('video');
    if(!video)throw new Error('播放器不存在');
    video.muted=true;
    const stream=video.captureStream(), tracks=stream.getAudioTracks();
    if(!tracks.length)throw new Error('播放器没有音轨');
    const context=new AudioContext({sampleRate:16000,latencyHint:'playback'});
    const url=URL.createObjectURL(new Blob([worklet],{type:'text/javascript'}));
    let tap,source;
    try {
        await context.audioWorklet.addModule(url);
        tap=new AudioWorkletNode(context,'monitor-tap');
        source=context.createMediaStreamSource(new MediaStream(tracks));
        const state={context,tap,source,video,tracks,parts:[],samples:0,missing:0,dropped:0,
            flushes:new Map(),last:Date.now(),lastMedia:video.currentTime,started:Date.now(),
            playError:null,pauses:0,mutedAt:tracks.some(t=>t.muted)?performance.now():null,mutedMs:0};
        for(const track of tracks) {
            track.addEventListener('mute',()=>{if(state.mutedAt==null)state.mutedAt=performance.now()});
            track.addEventListener('unmute',()=>{
                if(state.mutedAt!=null)state.mutedMs+=performance.now()-state.mutedAt;
                state.mutedAt=null;
            });
        }
        state.close=async()=>{
            source.disconnect();tap.disconnect();tap.port.onmessage=null;
            for(const track of stream.getTracks())track.stop();
            await context.close();if(window.__pcmMonitor===state)window.__pcmMonitor=null;
        };
        tap.port.onmessage=e=>{
            const chunk=new Float32Array(e.data.samples);
            state.parts.push(chunk);state.samples+=chunk.length;state.missing+=e.data.missing;
            // Python读取停顿时限制页面缓冲，丢失样本数显式返回，不伪装完整覆盖。
            while(state.samples>16000*75 && state.parts.length>1) {
                const old=state.parts.shift();state.samples-=old.length;state.dropped+=old.length;
            }
            if(e.data.id!=null) {
                const resolve=state.flushes.get(e.data.id);
                state.flushes.delete(e.data.id);if(resolve)resolve();
            }
        };
        source.connect(tap);tap.connect(context.destination); // Worklet输出始终为0，不外放。
        window.__pcmMonitor=state;
        await context.resume();
        state.last=Date.now();state.lastMedia=video.currentTime;
        return {sample_rate:context.sampleRate,started:state.last};
    } catch(error) {
        if(source)source.disconnect();if(tap)tap.disconnect();
        for(const track of stream.getTracks())track.stop();
        await context.close();window.__pcmMonitor=null;throw error;
    } finally {URL.revokeObjectURL(url)}
}'''

READ_PCM = '''async (seconds) => {
    const m=window.__pcmMonitor;
    if(!m)throw new Error('PCM采集器不存在');
    await new Promise(resolve=>setTimeout(resolve,seconds*1000));
    const id=Date.now()+Math.random();
    await new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{m.flushes.delete(id);reject(new Error('音频线程输出超时'))},3000);
        m.flushes.set(id,()=>{clearTimeout(timer);resolve()});m.tap.port.postMessage({flush:true,id});
    });
    if(document.querySelector('video')!==m.video)throw new Error('播放器元素已更换，需要重建采集');
    const samples=m.samples, floats=new Float32Array(samples);
    let offset=0;for(const part of m.parts){floats.set(part,offset);offset+=part.length}
    const pcm=new Int16Array(samples);
    for(let i=0;i<samples;i++)pcm[i]=Math.round(Math.max(-1,Math.min(1,floats[i]))*32767);
    const bytes=new Uint8Array(pcm.buffer);let binary='';
    for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
    const ended=Date.now(), media=m.video.currentTime;
    const perf=performance.now(), mutedMs=m.mutedMs+(m.mutedAt==null?0:perf-m.mutedAt);
    const result={data:btoa(binary),started:m.last,ended,sample_rate:m.context.sampleRate,
        muted:m.video.muted,paused:m.video.paused,track_live:m.tracks.every(t=>t.readyState==='live'),
        context_state:m.context.state,missing_samples:m.missing,dropped_samples:m.dropped,
        media_advanced:media-m.lastMedia,pauses:m.pauses,play_error:m.playError,
        track_muted:m.tracks.some(t=>t.muted),track_enabled:m.tracks.every(t=>t.enabled),
        track_muted_ms:mutedMs};
    m.parts=[];m.samples=0;m.missing=0;m.dropped=0;m.last=ended;m.lastMedia=media;m.pauses=0;
    m.mutedMs=0;if(m.mutedAt!=null)m.mutedAt=perf;
    return result;
}'''


async def start_pcm(page):
    result = await asyncio.wait_for(page.evaluate(START_PCM, WORKLET), 12)
    if result['sample_rate'] != 16000:
        raise RuntimeError(f'音频上下文采样率异常：{result["sample_rate"]}')
    return result


async def read_pcm(page, seconds):
    result = await asyncio.wait_for(page.evaluate(READ_PCM, seconds), seconds + 10)
    pcm = base64.b64decode(result.pop('data'))
    return pcm, result


async def close_pcm(page):
    await asyncio.wait_for(page.evaluate(
        'async()=>{if(window.__pcmMonitor)await window.__pcmMonitor.close()}'), 4)
