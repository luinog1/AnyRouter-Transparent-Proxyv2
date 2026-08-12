"""FastAPI entrypoint and Claude Code-compatible AgentRouter proxy."""

import os
import platform
import sys
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from backend.config import CUSTOM_HEADERS, DEBUG_MODE, HOP_BY_HOP_HEADERS, TARGET_BASE_URL

app = FastAPI()
PORT = int(os.getenv("PORT", "8000"))

http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=30.0, read=None, write=120.0, pool=30.0),
    follow_redirects=False,
)

# AgentRouter's WAF expects the Anthropic Messages request to resemble Claude
# Code rather than a generic Anthropic SDK request. Keep the versioned beta
# list in one place so it can be updated without touching the proxy logic.
AGENTROUTER_BETA = os.getenv(
    "AGENTROUTER_ANTHROPIC_BETA",
    "claude-code-20250219,interleaved-thinking-2025-05-14,effort-2025-11-24,redact-thinking-2026-02-12",
)
CLAUDE_CODE_UA = os.getenv("CLAUDE_CODE_USER_AGENT", "claude-cli/2.1.228 (external, sdk-cli)")
STAINLESS_PACKAGE = os.getenv("CLAUDE_STAINLESS_PACKAGE_VERSION", "0.94.0")


def forward_headers(request: Request) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in request.headers.items():
        key_lower = key.lower()
        if key_lower in HOP_BY_HOP_HEADERS or key_lower in {"host", "content-length"}:
            continue
        result[key] = value
    result.update(CUSTOM_HEADERS)
    return result


def apply_claude_code_wire_image(headers: dict[str, str]) -> dict[str, str]:
    """Make the upstream request look like Claude Code while preserving auth."""
    # Keep the real API credential supplied by Claude Code/CC-Switch. AgentRouter
    # accepts Anthropic-style x-api-key or Authorization depending on the client.
    headers["anthropic-version"] = headers.get("anthropic-version", "2023-06-01")
    headers["anthropic-beta"] = AGENTROUTER_BETA
    headers["anthropic-dangerous-direct-browser-access"] = "true"
    headers["x-app"] = "cli"
    headers["user-agent"] = CLAUDE_CODE_UA

    # The Stainless headers are part of the Claude Code wire image. Preserve any
    # headers already supplied by the current Claude Code client and only add the
    # stable package/runtime identifiers when absent.
    headers.setdefault("x-stainless-lang", "js")
    headers.setdefault("x-stainless-package-version", STAINLESS_PACKAGE)
    headers.setdefault("x-stainless-runtime", "node")
    headers.setdefault("x-stainless-runtime-version", os.getenv("CLAUDE_STAINLESS_RUNTIME_VERSION", "v24.3.0"))
    headers.setdefault("x-stainless-arch", platform.machine())
    headers.setdefault("x-stainless-os", sys.platform)
    return headers


def is_claude_messages(path: str) -> bool:
    return path.strip("/").lower() == "v1/messages"


@app.get("/")
@app.head("/")
async def root():
    return Response(status_code=200, headers={"x-proxy-status": "ok"})


@app.get("/health")
@app.head("/health")
async def health():
    return {"status": "healthy"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    start_time = time.time()
    body = await request.body()

    target_url = f"{TARGET_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        target_url += "?" + request.url.query

    headers = forward_headers(request)
    if is_claude_messages(path):
        headers = apply_claude_code_wire_image(headers)

    if DEBUG_MODE:
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host="0.0.0.0", port=PORT, proxy_headers=True)
