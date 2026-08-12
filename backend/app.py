"""FastAPI entrypoint and Claude Code-compatible AgentRouter proxy."""

import os
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from backend.claude_wire import claude_headers, transform_body
from backend.config import CUSTOM_HEADERS, DEBUG_MODE, HOP_BY_HOP_HEADERS, TARGET_BASE_URL
from backend.routers.admin import router as admin_router

app = FastAPI()
PORT = int(os.getenv("PORT", "8000"))

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0),
    follow_redirects=False,
)


def forward_headers(request: Request) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in request.headers.items():
        key_lower = key.lower()
        if key_lower in HOP_BY_HOP_HEADERS or key_lower in {"host", "content-length"}:
            continue
        result[key] = value
    result.update(CUSTOM_HEADERS)
    return result


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


app.include_router(admin_router)


async def proxy_request(path: str, request: Request):
    start_time = time.time()
    body = await request.body()
    target_url = f"{TARGET_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        target_url += "?" + request.url.query

    headers = forward_headers(request)
    if is_claude_messages(path):
        body = transform_body(body)
        headers = claude_headers(headers)
        headers["content-type"] = "application/json"
        headers.pop("content-length", None)
        print(f"[ClaudeWire] transformed /{path}: {len(body)} bytes")

    if DEBUG_MODE:
        print(f"[Proxy] {request.method} /{path} -> {target_url}")
    else:
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
            key: value for key, value in response.headers.items()
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


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def v1_proxy(path: str, request: Request):
    return await proxy_request(f"v1/{path}", request)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    return await proxy_request(path, request)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.app:app", host="0.0.0.0", port=PORT, proxy_headers=True)
