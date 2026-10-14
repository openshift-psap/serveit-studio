"""LoRA round-robin router for guidellm benchmarks.

Runs inside the guidellm workload pod (prebuilt into the image at
/usr/local/bin/lora-router). guidellm sends every request with a single
fixed ``model`` field (its backend resolves one model per run), so a
LoRA-enabled server would never exercise its adapters. This proxy rewrites
the ``model`` field per request, round-robin across the base model and the
LoRA adapter names, then relays the response unchanged — one guidellm run
produces a real mixed-adapter workload (including --max-loras LRU swapping
when there are more adapters than GPU-resident slots).

Built on aiohttp: the server and client share one event loop and the
streaming relay forwards first chunks as they arrive (no buffering), so the
proxy adds sub-millisecond latency to the TTFT path.

Environment:
    LORA_UPSTREAM    upstream URL (gateway / vLLM service), e.g. http://host:8080
    LORA_BASE_MODEL  base served-model name routed as-is
    LORA_ADAPTERS    comma-separated adapter names (e.g. agent-01,agent-02)
    LORA_PORT        listen port (default 8010)

Probe:  GET /lora-router/health  -> {"ok": true, "models": [...], "served": N}
"""

import itertools
import json
import os

import aiohttp
from aiohttp import web

UPSTREAM = os.environ.get('LORA_UPSTREAM', '').rstrip('/')
BASE_MODEL = os.environ.get('LORA_BASE_MODEL', '').strip()
ADAPTERS = [a.strip() for a in os.environ.get('LORA_ADAPTERS', '').split(',') if a.strip()]
MODELS = ([BASE_MODEL] if BASE_MODEL else []) + ADAPTERS
PORT = int(os.environ.get('LORA_PORT', '8010'))

if not UPSTREAM or not MODELS:
    raise SystemExit('LORA_UPSTREAM and (LORA_BASE_MODEL or LORA_ADAPTERS) are required')

_counter = itertools.count()
_served = itertools.count()

# One shared client session: connection pooling, keep-alive to the upstream.
_session: aiohttp.ClientSession | None = None

HOP_HEADERS = {'host', 'content-length', 'connection', 'transfer-encoding',
               'keep-alive', 'upgrade', 'proxy-authorization', 'te', 'trailers'}


def _forward_headers(request: web.Request) -> dict:
    return {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}


def next_model() -> str:
    return MODELS[next(_counter) % len(MODELS)]


async def health(_request: web.Request) -> web.Response:
    return web.json_response({
        'ok': True,
        'upstream': UPSTREAM,
        'models': MODELS,
        'served': next(_served),
    })


async def completions(request: web.Request) -> web.StreamResponse:
    """POST /v1/completions and /v1/chat/completions — rewrite model, relay."""
    try:
        body = json.loads(await request.read())
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    body['model'] = next_model()
    next(_served)
    payload = json.dumps(body).encode()

    upstream_req = _session.post(
        UPSTREAM + request.path,
        data=payload,
        headers={**_forward_headers(request), 'content-type': 'application/json'},
    )

    if body.get('stream'):
        # Streaming: relay chunks as they arrive — TTFT measurement depends
        # on first-chunk latency, so no buffering anywhere in the path.
        upstream = await upstream_req
        resp = web.StreamResponse(
            status=upstream.status,
            headers={k: v for k, v in upstream.headers.items()
                     if k.lower() not in HOP_HEADERS},
        )
        await resp.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
        finally:
            upstream.release()
        await resp.write_eof()
        return resp

    upstream = await upstream_req
    body = await upstream.read()
    return web.Response(
        status=upstream.status,
        body=body,
        content_type=upstream.headers.get('content-type', 'application/json'),
    )


async def passthrough(request: web.Request) -> web.Response:
    """Everything else (models, health, metrics) goes unchanged."""
    upstream = await _session.request(
        request.method,
        UPSTREAM + request.path_qs,
        data=await request.read(),
        headers=_forward_headers(request),
    )
    body = await upstream.read()
    return web.Response(
        status=upstream.status,
        body=body,
        content_type=upstream.headers.get('content-type', 'application/json'),
    )


async def on_startup(app: web.Application):
    global _session
    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_read=600),
        connector=aiohttp.TCPConnector(limit=0),  # unbounded — benchmark concurrency
    )


async def on_cleanup(_app: web.Application):
    if _session:
        await _session.close()


def main():
    app = web.Application()
    app.router.add_get('/lora-router/health', health)
    app.router.add_post('/v1/completions', completions)
    app.router.add_post('/v1/chat/completions', completions)
    app.router.add_route('*', '/{path:.*}', passthrough)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host='0.0.0.0', port=PORT,
                access_log=None, print=None)


if __name__ == '__main__':
    main()
