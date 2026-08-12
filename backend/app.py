"""AnyRouter / AgentRouter transparent proxy service."""

from contextlib import asynccontextmanager
from typing import Optional
import asyncio
import json
import os
import socket
import time
import uuid

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from .config import TARGET_BASE_URL, DEBUG_MODE, PORT, ENABLE_DASHBOARD, DASHBOARD_API_KEY, CUSTOM_HEADERS
from .services.stats import (
    record_request_start,
    record_request_success,
    record_request_error,
    periodic_stats_update,
    cleanup_stale_requests,
)
from .services.proxy import process_request_body, filter_response_headers, prepare_forward_headers
from .services.proxy_pool import ProxyPool
from .utils.encoding import ensure_unicode
from .routers.admin import router as admin_router

http_client: Optional[httpx.AsyncClient] = None
proxy_clients: dict[str, httpx.AsyncClient] = {}
proxy_client_last_used: dict[str, float] = {}
proxy_clients_lock = asyncio.Lock()
MAX_CACHED_PROXY_CLIENTS = int(os.getenv("MAX_CACHED_PROXY_CLIENTS", "16"))
proxy_pool = ProxyPool()


async def get_proxy_client(proxy_url: str) -> httpx.AsyncClient:
    """Return a reusable HTTP client for one upstream proxy."""
    async with proxy_clients_lock:
        client = proxy_clients.get(proxy_url)
        if client is None:
            client = httpx.AsyncClient(
                proxy=proxy_url,
                follow_redirects=False,
                timeout=httpx.Timeout(90.0, connect=15.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0),
            )
            proxy_clients[proxy_url] = client
        proxy_client_last_used[proxy_url] = time.monotonic()

        if len(proxy_clients) > MAX_CACHED_PROXY_CLIENTS:
            oldest = min(proxy_client_last_used, key=proxy_client_last_used.get)
            if oldest != proxy_url:
                old_client = proxy_clients.pop(oldest)
                proxy_client_last_used.pop(oldest, None)
                await old_client.aclose()
        return client


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    app.state.boot_id = str(uuid.uuid4())
    app.state.started_at = int(time.time())

    stats_task = asyncio.create_task(periodic_stats_update())
    cleanup_task = asyncio.create_task(cleanup_stale_requests())

    print("=" * 60)
    print("AgentRouter Transparent Proxy")
    print(f"  Base URL: {TARGET_BASE_URL}")
    print(f"  Server Port: {PORT}")
    print(f"  Proxy Pool: {proxy_pool.enabled} ({len(proxy_pool._states)} proxies)")
    print(f"  Dashboard: {ENABLE_DASHBOARD}")
    print("=" * 60)

    http_proxy = os.getenv("HTTP_PROXY")
    https_proxy = os.getenv("HTTPS_PROXY")
    mounts = {}
    if http_proxy:
        if "://" not in http_proxy:
            http_proxy = "http://" + http_proxy
        mounts["http://"] = httpx.AsyncHTTPTransport(proxy=http_proxy)
    if https_proxy:
        if "://" not in https_proxy:
            https_proxy = "http://" + https_proxy
        mounts["https://"] = httpx.AsyncHTTPTransport(proxy=https_proxy)

    http_client = httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(90.0, connect=15.0),
        mounts=mounts or None,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30.0),
    )

    yield

    stats_task.cancel()
    cleanup_task.cancel()
    for task in (stats_task, cleanup_task):
        try:
            await task
        except asyncio.CancelledError:
            pass

    if http_client:
        await http_client.aclose()
    for client in list(proxy_clients.values()):
        await client.aclose()
    proxy_clients.clear()
    proxy_client_last_used.clear()


app = FastAPI(title="AgentRouter Transparent Proxy", version="2.0", lifespan=lifespan)
app.include_router(admin_router)


@app.get("/health")
async def health_check(request: Request):
    return Response(
        content=json.dumps({
            "status": "healthy",
            "service": "agentrouter-transparent-proxy",
            "boot_id": request.app.state.boot_id,
            "started_at": request.app.state.started_at,
            "target": TARGET_BASE_URL,
            "proxy_pool": proxy_pool.stats(),
        }),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/favicon.ico")
async def favicon():
    # Browser requests to the public URL must never be sent upstream.
    return Response(status_code=204, headers={"Cache-Control": "public, max-age=86400"})


@app.api_route("/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def root_redirect(request: Request):
    accept = request.headers.get("accept", "")
    if request.method == "GET" and ("text/html" in accept or "application/xhtml+xml" in accept):
        return RedirectResponse(url="/admin", status_code=307)
    return await proxy("", request)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    start_time = time.time()
    body = await request.body()

    request_id = None
    if not path.startswith("api/admin") and not path.startswith("admin"):
        request_id = await record_request_start(path, request.method, len(body))

    target_url = f"{TARGET_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        target_url += "?" + request.url.query

    # Only modify Claude Messages payloads when explicitly configured.
    if path in {"v1/messages", "v1/messages/"}:
        body = process_request_body(body)

    incoming_headers = list(request.headers.items())
    client_host = request.client.host if request.client else None
    forward_headers = prepare_forward_headers(incoming_headers, client_host)

    selected_proxy = await proxy_pool.choose()
    client = await get_proxy_client(selected_proxy) if selected_proxy else http_client

    if DEBUG_MODE:
        print(f"[Proxy] {request.method} /{path} -> {target_url} proxy={bool(selected_proxy)}")
    else:
        print(f"[Proxy] {request.method} /{path} -> {target_url} proxy={bool(selected_proxy)}")

    response_time = 0.0
    bytes_received = 0
    error_response_content = b""

    try:
        req = client.build_request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            content=body,
        )
        resp = await client.send(req, stream=True)
        response_time = time.time() - start_time

        if selected_proxy and resp.status_code < 500:
            await proxy_pool.report_success(selected_proxy, response_time)
        elif selected_proxy and resp.status_code >= 500:
            # A 5xx can be an upstream/model error rather than a dead proxy;
            # don't immediately evict the proxy.
            await proxy_pool.report_success(selected_proxy, response_time)

        response_headers = filter_response_headers(resp.headers.items())

        async def iter_response():
            nonlocal bytes_received, error_response_content
            try:
                async for chunk in resp.aiter_bytes():
                    bytes_received += len(chunk)
                    if resp.status_code >= 400 and len(error_response_content) < 50 * 1024:
                        error_response_content += chunk
                    yield chunk
            finally:
                pass

        async def close_and_record():
            await resp.aclose()
            if not request_id:
                return
            if resp.status_code < 400:
                await record_request_success(
                    request_id, path, request.method, bytes_received,
                    response_time, resp.status_code,
                )
            else:
                response_content = ensure_unicode(error_response_content) if error_response_content else None
                await record_request_error(
                    request_id, path, request.method,
                    f"HTTP {resp.status_code}: {resp.reason_phrase}",
                    response_time, response_content, resp.status_code,
                )

        return StreamingResponse(
            iter_response(),
            status_code=resp.status_code,
            headers=response_headers,
            background=BackgroundTask(close_and_record),
        )

    except (httpx.ProxyError, httpx.ConnectError) as exc:
        if selected_proxy:
            await proxy_pool.report_failure(selected_proxy)
        if request_id:
            await record_request_error(
                request_id, path, request.method, str(exc),
                time.time() - start_time, None, 502,
            )
        return Response(content=f"Upstream connection failed: {exc}", status_code=502)
    except httpx.RequestError as exc:
        if request_id:
            await record_request_error(
                request_id, path, request.method, str(exc),
                time.time() - start_time, None, 502,
            )
        return Response(content=f"Upstream request failed: {exc}", status_code=502)


def _choose_available_port(preferred_port: int) -> int:
    candidates = [preferred_port]
    fallback_port = int(os.getenv("FALLBACK_PORT", "8088"))
    if fallback_port not in candidates:
        candidates.append(fallback_port)
    candidates.append(0)
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred_port


if __name__ == "__main__":
    import uvicorn
    reload_enabled = DEBUG_MODE and os.getenv("ENABLE_RELOAD", "false").lower() in ("true", "1", "yes")
    selected_port = _choose_available_port(PORT)
    uvicorn.run("backend.app:app", host="0.0.0.0", port=selected_port, reload=reload_enabled)
