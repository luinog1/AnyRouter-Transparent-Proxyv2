from __future__ import annotations

import hashlib
import json
import re
from typing import Any

CLAUDE_CODE_VERSION = "2.1.219"
CLAUDE_CODE_SDK_VERSION = "0.94.0"
CLAUDE_CODE_RUNTIME_VERSION = "v26.3.0"
CLAUDE_CODE_USER_AGENT = f"claude-cli/{CLAUDE_CODE_VERSION} (external, sdk-cli)"
ANTHROPIC_VERSION = "2023-06-01"
CCH_SALT = "59cf53e54c78"
CCH_SEED = 0x6E52736AC806831E
SDK_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
REMOVAL_ANCHORS = ("github.com/anomalyco/opencode", "opencode.ai/docs", "github.com/cline/cline", "github.com/getcursor/cursor", "continue.dev")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                    return block["text"]
    return ""


def _billing(messages: list[dict[str, Any]]) -> str:
    text = _first_user_text(messages)
    picks = "".join(text[p] if p < len(text) else "\0" for p in (4, 7, 20))
    suffix = _sha(f"{CCH_SALT}{picks}{CLAUDE_CODE_VERSION}")[:3]
    cch = _sha(text)[:5]
    return f"x-anthropic-billing-header: cc_version={CLAUDE_CODE_VERSION}.{suffix}; cc_entrypoint=sdk-cli; cch={cch};"


def _normalize_system(body: dict[str, Any], messages: list[dict[str, Any]]) -> None:
    system = body.get("system")
    blocks: list[dict[str, Any]] = []
    if isinstance(system, str) and system:
        blocks = [{"type": "text", "text": system}]
    elif isinstance(system, list):
        blocks = [dict(x) for x in system if isinstance(x, dict)]
    out: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            out.append(block)
            continue
        text = block["text"]
        paragraphs = text.split("\n\n")
        paragraphs = [p for p in paragraphs if not any(a.lower() in p.lower() for a in REMOVAL_ANCHORS) and not p.lstrip().lower().startswith("you are opencode")]
        text = "\n\n".join(paragraphs).replace("if OpenCode honestly", "if the assistant honestly").replace("Here is some useful information about the environment you are running in:", "Environment context you are running in:")
        if text:
            out.append({**block, "text": text})
    if not any(x.get("type") == "text" and x.get("text") == SDK_IDENTITY for x in out):
        out.insert(0, {"type": "text", "text": SDK_IDENTITY})
    out.insert(0, {"type": "text", "text": _billing(messages)})
    body["system"] = out


def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & 0xFFFFFFFFFFFFFFFF


def _xxh64(data: bytes, seed: int = CCH_SEED) -> int:
    mask = 0xFFFFFFFFFFFFFFFF
    p, n = 0, len(data)
    p1, p2, p3, p4, p5 = 11400714785074694791, 14029467366897019727, 1609587929392839161, 9650029242287828579, 2870177450012600261
    def rd64(i): return int.from_bytes(data[i:i+8], "little")
    def rd32(i): return int.from_bytes(data[i:i+4], "little")
    def rnd(acc, value): return (_rotl((acc + value * p2) & mask, 31) * p1) & mask
    if n >= 32:
        v1, v2, v3, v4 = (seed + p1 + p2) & mask, (seed + p2) & mask, seed & mask, (seed - p1) & mask
        while p <= n - 32:
            v1 = rnd(v1, rd64(p)); p += 8
            v2 = rnd(v2, rd64(p)); p += 8
            v3 = rnd(v3, rd64(p)); p += 8
            v4 = rnd(v4, rd64(p)); p += 8
        h = (_rotl(v1,1) + _rotl(v2,7) + _rotl(v3,12) + _rotl(v4,18)) & mask
        for v in (v1,v2,v3,v4):
            v = rnd(0, v); h ^= v; h = (h * p1 + p4) & mask
    else:
        h = (seed + p5) & mask
    h = (h + n) & mask
    while p + 8 <= n:
        h ^= rnd(0, rd64(p)); h = (_rotl(h,27) * p1 + p4) & mask; p += 8
    if p + 4 <= n:
        h ^= (rd32(p) * p1) & mask; h = (_rotl(h,23) * p2 + p3) & mask; p += 4
    while p < n:
        h ^= (data[p] * p5) & mask; h = (_rotl(h,11) * p1) & mask; p += 1
    h ^= h >> 33; h = (h * p2) & mask; h ^= h >> 29; h = (h * p3) & mask; h ^= h >> 32
    return h & mask


def transform_body(raw: bytes) -> bytes:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw
    if not isinstance(body, dict):
        return raw
    messages = [m for m in body.get("messages", []) if isinstance(m, dict)]
    body["messages"] = messages
    _normalize_system(body, messages)
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    encoded = re.sub(r"cch=[0-9a-f]{5};", "cch=00000;", encoded, count=1)
    token = _xxh64(encoded.encode("utf-8")) & 0xFFFFF
    encoded = encoded.replace("cch=00000;", f"cch={token:05x};", 1)
    return encoded.encode("utf-8")


def claude_headers(headers: dict[str, str]) -> dict[str, str]:
    out = dict(headers)
    out.update({
        "anthropic-version": ANTHROPIC_VERSION,
        "anthropic-beta": "claude-code-20250219,interleaved-thinking-2025-05-14,effort-2025-11-24,redact-thinking-2026-02-12",
        "anthropic-dangerous-direct-browser-access": "true",
        "x-app": "cli",
        "user-agent": CLAUDE_CODE_USER_AGENT,
        "x-stainless-retry-count": "0",
        "x-stainless-timeout": "600",
        "x-stainless-lang": "js",
        "x-stainless-package-version": CLAUDE_CODE_SDK_VERSION,
        "x-stainless-os": "MacOS",
        "x-stainless-arch": "arm64",
        "x-stainless-runtime": "node",
        "x-stainless-runtime-version": CLAUDE_CODE_RUNTIME_VERSION,
        "accept-encoding": "gzip, deflate, br, zstd",
    })
    return out
