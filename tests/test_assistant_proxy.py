import asyncio

import httpx
import pytest

from bilibili_live_monitor.assistant_api import create_sidecar, end_to_end


class Chunks(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b'event: item\n'
        await asyncio.sleep(0)
        yield b'data: {"room_id":"123456"}\n\n'

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_proxy_method_query_body_sse_and_close(tmp_path):
    stream = Chunks()
    async def handler(request):
        assert request.method == 'POST'
        assert request.url.query == b'room_id=123456&x=a%20b'
        assert await request.aread() == b'input-body'
        assert 'x-hop' not in request.headers
        return httpx.Response(201, headers={'content-type': 'text/event-stream',
                                           'cache-control': 'no-cache', 'connection': 'x-hop', 'x-hop': 'private'}, stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_sidecar(tmp_path/'state.sqlite', client=upstream, static_dir=tmp_path/'missing')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://sidecar') as client:
            response = await client.post('/api/events?room_id=123456&x=a%20b', content=b'input-body',
                                         headers={'connection': 'x-hop', 'x-hop': 'secret'})
            assert response.status_code == 201
            assert response.headers['content-type'] == 'text/event-stream'
            assert response.headers['cache-control'] == 'no-cache'
            assert 'x-hop' not in response.headers
            assert response.content == b'event: item\ndata: {"room_id":"123456"}\n\n'
            assert stream.closed


@pytest.mark.asyncio
async def test_proxy_disconnect_closes_without_buffering(tmp_path):
    stream = Chunks()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=stream))) as upstream:
        app = create_sidecar(tmp_path/'state.sqlite', client=upstream, static_dir=tmp_path/'missing')
        received = asyncio.Event()
        sent = []
        request_sent = False
        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            await received.wait()
            return {'type': 'http.disconnect'}
        async def send(message):
            sent.append(message)
            if message['type'] == 'http.response.body' and message.get('body'):
                received.set()
        scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                 'method': 'GET', 'scheme': 'http', 'path': '/api/events',
                 'raw_path': b'/api/events', 'query_string': b'', 'headers': [],
                 'server': ('test', 80), 'client': ('test', 1)}
        await app(scope, receive, send)
        assert stream.closed
        assert any(m.get('body') == b'event: item\n' for m in sent)


def test_hop_headers_case_and_static(tmp_path):
    assert end_to_end([(b'Connection', b'X-Hop'), (b'X-Hop', b'bad'), (b'Last-Event-ID', b'5')]) == [(b'Last-Event-ID', b'5')]


@pytest.mark.asyncio
async def test_static_and_upstream_resource_metrics(tmp_path):
    (tmp_path/'index.html').write_text('<h1>synthetic frontend</h1>')
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=httpx.ByteStream(
                b'{"path":"/assets/resource_metrics.json"}')))) as upstream:
        app = create_sidecar(tmp_path/'assistant.sqlite', client=upstream, static_dir=tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            assert 'synthetic frontend' in (await client.get('/')).text
            assert (await client.get('/assets/resource_metrics.json')).json() == {'path': '/assets/resource_metrics.json'}
