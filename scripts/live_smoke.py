"""显式运行真实直播短测，证据只写入本地目录。"""
import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx


async def main(args):
    async with httpx.AsyncClient(base_url=args.base_url, timeout=80) as client:
        schema = (await client.get('/openapi.json')).json()
        assert '/api/rooms' in schema['paths']
        response = await client.get(f'/api/discover?limit={min(100, max(40, args.rooms * 3))}')
        response.raise_for_status()
        candidates = response.json()
        attempts = []
        print('发现候选', len(candidates), flush=True)
        current = (await client.get('/api/state')).json()
        # 优先恢复已知房间，再尝试新候选，保持重启前的监控来源。
        known = [{'url': r['url'], 'title': r['title']} for r in current['rooms']]
        candidates = list({c['url']: c for c in [*known, *candidates]}.values())
        baseline = {r['room_id']: max((s['seq'] for s in r['segments']), default=0)
                    for r in current['rooms']}
        if args.resume:
            previous = json.loads(Path(args.resume).read_text())
            baseline = previous['baseline']
            attempts = previous['attempts']
        active = [r for r in current['rooms'] if r['status'] in ('capturing', 'reconnecting')]
        index = 0
        while len(active) < args.rooms and index < len(candidates):
            active_urls = {r['url'] for r in active}
            while index < len(candidates) and candidates[index]['url'] in active_urls:
                index += 1
            batch = candidates[index:index + min(args.rooms-len(active), 5)]
            index += len(batch)

            async def add(candidate):
                result = await client.post('/api/rooms', json={'url': candidate['url'], 'chunk_seconds':30,
                                                            'asr_enabled':True, 'record_mode':'none'})
                record = {'candidate': candidate, 'status': result.status_code, 'response': result.json()}
                attempts.append(record)
                print('添加', candidate['url'], result.status_code, flush=True)

            await asyncio.gather(*(add(candidate) for candidate in batch))
            current = (await client.get('/api/state')).json()
            active = [r for r in current['rooms'] if r['status'] in ('capturing', 'reconnecting')]
        print('活跃房间', len(active), flush=True)
        deadline = time.monotonic() + args.wait_seconds
        while time.monotonic() < deadline:
            current = (await client.get('/api/state')).json()
            complete = [r for r in current['rooms'] if r['status'] == 'capturing'
                        and len([s for s in r['segments'] if s['seq'] > baseline.get(r['room_id'], 0)
                                 and s['status'] in ('done','empty','silent','truncated')
                                 and s.get('coverage_ratio') is not None
                                 and 0.90 <= s['coverage_ratio'] <= 1.20
                                 and s['duration_seconds'] >= 27 and s['muted'] and not s['paused']
                                 and s.get('media_advanced', 0) >= s['wall_seconds'] * .8
                                 and s.get('missing_samples', 1601) <= 1600
                                 and not s.get('dropped_samples')]) >= args.segments]
            print(f'每路{args.segments}段已完成', len(complete), '队列', current['queue_depth'], flush=True)
            if len(complete) >= args.rooms:
                break
            await asyncio.sleep(5)
        cursor = max(0, current['cursor'] - 1)
        replay = []
        async with client.stream('GET', '/api/events', headers={'Last-Event-ID':str(cursor)}) as stream:
            async for line in stream.aiter_lines():
                if line.startswith('id: '):
                    replay.append(int(line[4:]))
                    break
        passed = len(complete) >= args.rooms and replay == [cursor + 1]
        evidence = {'attempts':attempts, 'state':current, 'baseline':baseline,
                    'replay_after':cursor, 'replayed_ids':replay, 'passed':passed}
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        summary = [{'room_id':r['room_id'], 'status':r['status'],
                    'segments':[(s['seq'],s['status'],s.get('infer_ms')) for s in r['segments'][-2:]]}
                   for r in current['rooms']]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print('证据', output)
        if not passed:
            raise RuntimeError('实机短测未满足全部条件，查看本地证据；不计作通过')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='十路真实直播短测（会创建直播标签页并消耗本机算力）')
    parser.add_argument('--base-url', default='http://127.0.0.1:8765')
    parser.add_argument('--rooms', type=int, default=10)
    parser.add_argument('--segments', type=int, default=2)
    parser.add_argument('--wait-seconds', type=int, default=130)
    parser.add_argument('--output', default='.local/live_smoke.json')
    parser.add_argument('--resume', help='沿用同一次运行的原始序号基线继续检查，不改写前一份证据')
    args = parser.parse_args()
    if args.rooms < 1 or args.wait_seconds < 1 or args.segments < 1:
        parser.error('rooms、segments与wait-seconds必须为正整数')
    asyncio.run(main(args))
