"""FastAPI entrypoint and transparent upstream proxy."""

import os
import time
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from backend.config import (
    CUSTOM_HEADERS,
    DEBUG_MODE,
    HOP_BY_HOP_HEADERS,
    TARGET_BASE_URL,
)

app = FastAPI()
PORT = int(os.getenv("PORT", "8000"))

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0),
    follow_redirects=False,
    http2=True,
)


def prepare_forward_headers(headers, client_host: Optional[str] = None) -> dict:
    result = {}
    for key, value in headers:
        key_lower = key.lower()
        if key_lower in HOP_BY_HOP_HEADERS or key_lower == "host":
            continue
        result[key] = value
    result.update(CUSTOM_HEADERS)
    return result


def is_claude_messages(path: str) -> bool:
    return path.strip("/").lower() in {"v1/messages", "v1/messages/"}


def ensure_agentrouter_claude_headers(headers: dict) -> dict:
    """Preserve Claude Code's real fingerprint and fill only safe defaults."""
    lowered = {k.lower() for k in headers}
    if "anthropic-version" not in lowered:
        headers["anthropic-version"] = "2023-06-01"
    if "user-agent" not in lowered:
        headers["user-agent"] = "claude-code"
    return headers


@app.get("/")
async def root():
    return {"status": "ok", "service": "AnyRouter Transparent Proxy", "target": TARGET_BASE_URL}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.api_route("/admin", methods=["GET"])
async def admin_redirect():
    return RedirectResponse(url="/admin", status_code=307)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    start_time = time.time()
    body = await request.body()

    target_url = f"{TARGET_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        target_url += "?" + request.url.query

    forward_headers = prepare_forward_headers(list(request.headers.items()))
    if is_claude_messages(path):
        forward_headers = ensure_agentrouter_claude_headers(forward_headers)

    if DEBUG_MODE:
        print(f"[Proxy] {request.method} /{path} -> {target_url}")

    try:
        req = http_client.build_request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            content=body,
        )
        resp = await http_client.send(req, stream=True)
        elapsed = time.time() - start_time
        print(
            f"[Upstream] {request.method} /{path} -> {resp.status_code} "
            f"{elapsed:.3f}s content-type={resp.headers.get('content-type', '')}"
        )

        response_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in HOP_BY_HOP_HEADERS
        }

        if resp.status_code >= 400:
            content = await resp.aread()
            await resp.aclose()
            return Response(
                content=content,
                status_code=resp.status_code,
                headers=response_headers,
                media_type=resp.headers.get("content-type"),
            )

        async def stream_upstream():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(
            stream_upstream(),
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("content-type"),
        )
    except httpx.TimeoutException as exc:
        print(f"[Upstream] timeout {request.method} /{path}: {exc}")
        return JSONResponse({"error": "upstream timeout"}, status_code=504)
    except httpx.HTTPError as exc:
        print(f"[Upstream] error {request.method} /{path}: {exc}")
        return JSONResponse({"error": "upstream connection error"}, status_code=502)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "backend.app:app",
        host="0.0.0.0",
        port=PORT,
        proxy_headers=True,
    )
