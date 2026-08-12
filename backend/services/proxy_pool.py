"""Upstream proxy pool with health scoring and circuit breaking.

The proxy list is deliberately NOT committed to the repository. Load it from
PROXY_POOL_FILE (recommended) or PROXY_POOL (newline-separated URLs).
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse


@dataclass
class ProxyState:
    url: str
    successes: int = 0
    failures: int = 0
    latency_ema: float = 1.0
    consecutive_failures: int = 0
    disabled_until: float = 0.0
    last_used: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.disabled_until

    @property
    def score(self) -> float:
        total = self.successes + self.failures
        reliability = (self.successes + 2) / (total + 4)
        latency_factor = 1.0 / (1.0 + max(self.latency_ema, 0.0))
        return reliability * latency_factor


class ProxyPool:
    def __init__(self) -> None:
        self._states: dict[str, ProxyState] = {}
        self._lock = asyncio.Lock()
        self.enabled = False
        self.cooldown_seconds = int(os.getenv("PROXY_COOLDOWN_SECONDS", "30"))
        self.max_failures = int(os.getenv("PROXY_MAX_CONSECUTIVE_FAILURES", "3"))
        self.refresh()

    def refresh(self) -> None:
        values: list[str] = []
        file_path = os.getenv("PROXY_POOL_FILE", "/app/data/all_proxies.txt")
        env_value = os.getenv("PROXY_POOL", "")

        if env_value:
            values.extend(env_value.splitlines())

        try:
            if os.path.exists(file_path):
                with open(file_path, "r", encoding="utf-8") as fh:
                    values.extend(fh.read().splitlines())
        except OSError as exc:
            print(f"[ProxyPool] Failed to read {file_path}: {exc}")

        clean: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            if "://" not in value:
                value = "http://" + value
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
                continue
            if value not in seen:
                seen.add(value)
                clean.append(value)

        self._states = {url: self._states.get(url, ProxyState(url)) for url in clean}
        self.enabled = bool(self._states)
        print(f"[ProxyPool] Loaded {len(self._states)} upstream proxies; enabled={self.enabled}")

    async def choose(self) -> Optional[str]:
        if not self.enabled:
            return None
        async with self._lock:
            candidates = [s for s in self._states.values() if s.available]
            if not candidates:
                # Avoid total outage if every proxy is cooling down.
                candidates = list(self._states.values())
            if not candidates:
                return None
            # Prefer healthy/fast proxies while adding a small least-recently-used bias.
            candidates.sort(key=lambda s: (s.score, -s.last_used), reverse=True)
            selected = candidates[0]
            selected.last_used = time.monotonic()
            return selected.url

    async def report_success(self, url: Optional[str], latency: float) -> None:
        if not url:
            return
        state = self._states.get(url)
        if not state:
            return
        async with state.lock:
            state.successes += 1
            state.consecutive_failures = 0
            state.latency_ema = (state.latency_ema * 0.8) + (max(latency, 0.001) * 0.2)

    async def report_failure(self, url: Optional[str]) -> None:
        if not url:
            return
        state = self._states.get(url)
        if not state:
            return
        async with state.lock:
            state.failures += 1
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.max_failures:
                state.disabled_until = time.monotonic() + self.cooldown_seconds
                print(f"[ProxyPool] Cooling down proxy for {self.cooldown_seconds}s")

    def stats(self) -> dict:
        states = list(self._states.values())
        return {
            "enabled": self.enabled,
            "count": len(states),
            "healthy": sum(1 for s in states if s.available),
            "top": [
                {
                    "score": round(s.score, 4),
                    "latency_ema": round(s.latency_ema, 3),
                    "successes": s.successes,
                    "failures": s.failures,
                }
                for s in sorted(states, key=lambda x: x.score, reverse=True)[:10]
            ],
        }
