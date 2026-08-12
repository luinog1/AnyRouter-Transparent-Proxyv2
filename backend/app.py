"""FastAPI entrypoint and Claude Code-compatible AgentRouter proxy."""

import os
import platform
import sys
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from backend.config import CUSTOM_HEADERS, DEBUG_MODE, HOP_BY_HOP_HEADERS, TARGET_BASE_URL
from backend.routers.admin import router as admin_router

app = FastAPI()
PORT = int(os.getenv("PORT", "8000"))

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0),
    follow_redirects=False,
)


def forward_headers(request: Request) -> dict[str, str]:
    result = {}
    for key, value in request.headers.items():
        key_lower = key.lower()
        if key_lower in HOP_BY_HOP_HEADERS or key_lower in {"host", "content-length"}:
            continue
        result[key] = value
    result.update(CUSTOM_HEADERS)
    return result


def apply_dynamic_claude_code_wire_image(headers: dict[str, str]) -> dict[str, str]:
    """Apply the Claude Code-compatible AgentRouter wire image."""
    headers["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
    headers["anthropic-beta"] = headers.get(
        "anthropic-beta",
        "claude-code-20250219,interleaved-thinking-2025-05-14,effort-2025-11-24,redact-thinking-2026-02-12",
    )
    headers["anthropic-dangerous-direct-browser-access"] = headers.get(
        "anthropic-dangerous-direct-browser-access", "true"
    )
    headers["x-app"] = headers.get("x-app", "cli")
    headers.setdefault("user-agent", os.getenv("CLAUDE_CODE_USER_AGENT", "claude-cli/2.1.228 (external, sdk-cli)"))
    headers.setdefault("x-stainless-lang", "js")
    headers.setdefault("x-stainless-package-version", os.getenv("CLAUDE_STAINLESS_PACKAGE_VERSION", "0.94.0"))
    headers.setdefault("x-stainless-runtime", "node")
    headers.setdefault("x-stainless-runtime-version", os.getenv("CLAUDE_STAINLESS_RUNTIME_VERSION", "v24.3.0"))
    headers.setdefault("x-stainless-arch", platform.machine())
    headers.setdefault("x-stainless-os", sys.platform)
    return headers


def is_claude_messages(path: str) -> bool:
    return path.strip("/").lower() == "v1/messages"


@app.get("/")
async def root():
    return RedirectResponse(url="/admin/", status_code=307)


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.head("/api/hello")
async def api_hello_head():
    return Response(status_code=204)


@app.get("/api/hello")
async def api_hello_get():
    return {"status": "ok"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


# Keep the original dashboard API/static routes before the proxy catch-all.
app.include_router(admin_router)


async def proxy_request(path: str, request: Request):
    start_time = time.time()
    body = await request.body()

    target_url = f"{TARGET_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        target_url += "?" + request.url.query

    headers = forward_headers(request)
    if is_claude_messages(path):
        headers = apply_dynamic_claude_code_wire_image(headers)

    print(f"[Proxy] {request.method} /{path} -> {target_url}")

    try:
        response = await http_client.send(
            http_client.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            ),
            stream=True,
        )
        elapsed = time.time() - start_time
        print(
            f"[Upstream] {request.method} /{path} -> {response.status_code} "
            f"{elapsed:.3f}s content-type={response.headers.get('content-type', '')}"
        )

        response_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }

        if response.status_code >= 400:
            content = await response.aread()
            await response.aclose()
            return Response(
                content=content,
                status_code=response.status_code,
                headers=response_headers,
                media_type=response.headers.get("content-type"),
            )

        async def stream_upstream():
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            stream_upstream(),
            status_code=response.status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type"),
        )
    except httpx.TimeoutException as exc:
        print(f"[Upstream] timeout {request.method} /{path}: {exc}")
        return JSONResponse({"error": "upstream timeout"}, status_code=504)
    except httpx.HTTPError as exc:
        print(f"[Upstream] error {request.method} /{path}: {exc}")
        return JSONResponse({"error": "upstream connection error"}, status_code=502)


# Explicit /v1 route prevents another mounted router from producing a local 405.
# It is intentionally registered before the generic catch-all.
@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def v1_proxy(path: str, request: Request):
    return await proxy_request(f"v1/{path}", request)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    # /api/hello is handled locally above; all remaining paths use the upstream proxy.
    return await proxy_request(path, request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host="0.0.0.0", port=PORT, proxy_headers=True)
