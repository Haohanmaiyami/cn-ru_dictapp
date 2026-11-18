"""Shared AI limits for the current single-worker Uvicorn deployment.

State is process-local and resets on restart. Before adding workers/replicas,
move these counters to shared storage. These limits are not a spending cap.
"""

from collections import deque
from dataclasses import dataclass, field
from ipaddress import ip_address
from math import ceil
from threading import Lock
from time import monotonic
from typing import Callable

from fastapi import HTTPException, Request


@dataclass
class _ClientState:
    attempts: deque[float] = field(default_factory=deque)
    in_flight: bool = False


class AILimiter:
    MINUTE_LIMIT = 5
    WINDOW_LIMIT = 60
    WINDOW_SECONDS = 3 * 60 * 60
    MAX_IN_FLIGHT = 2
    MAX_CLIENTS = 4096

    def __init__(self, clock: Callable[[], float] = monotonic):
        self._clock = clock
        self._clients: dict[str, _ClientState] = {}
        self._in_flight = 0
        self._lock = Lock()

    @staticmethod
    def _reject(status: int, detail: str, seconds: float):
        raise HTTPException(
            status_code=status,
            detail=detail,
            headers={"Retry-After": str(max(1, ceil(seconds)))},
        )

    def reserve(self, client_ip: str) -> None:
        # No await between checking limits and reserving a slot. The lock also
        # keeps this safe if called from another thread in the same process.
        with self._lock:
            now = self._clock()
            cutoff = now - self.WINDOW_SECONDS
            for key, state in list(self._clients.items()):
                while state.attempts and state.attempts[0] <= cutoff:
                    state.attempts.popleft()
                if not state.attempts and not state.in_flight:
                    del self._clients[key]

            state = self._clients.get(client_ip)
            if state is not None:
                if state.in_flight:
                    self._reject(429, "Предыдущий перевод ещё выполняется.", 2)
                if len(state.attempts) >= self.WINDOW_LIMIT:
                    self._reject(
                        429, "Достигнут лимит 60 AI-запросов за 3 часа.",
                        state.attempts[0] + self.WINDOW_SECONDS - now,
                    )
                recent = [t for t in state.attempts if t > now - 60]
                if len(recent) >= self.MINUTE_LIMIT:
                    self._reject(
                        429, "Достигнут лимит 5 AI-запросов в минуту.",
                        recent[0] + 60 - now,
                    )

            if self._in_flight >= self.MAX_IN_FLIGHT:
                self._reject(503, "AI сейчас занят. Попробуйте позже.", 2)
            if state is None:
                if len(self._clients) >= self.MAX_CLIENTS:
                    self._reject(503, "AI сейчас занят. Попробуйте позже.", 60)
                state = _ClientState()
                self._clients[client_ip] = state

            # Count attempts before invoking the provider, including failures:
            # a timed-out request may already have incurred provider charges.
            state.attempts.append(now)
            state.in_flight = True
            self._in_flight += 1

    def release(self, client_ip: str) -> None:
        with self._lock:
            state = self._clients.get(client_ip)
            if state is not None and state.in_flight:
                state.in_flight = False
                self._in_flight -= 1


def get_ai_client_ip(request: Request) -> str:
    # Caddy MUST overwrite this header with {remote_host}, never append to it.
    # The backend must remain bound to 127.0.0.1:8000 / a private Docker network.
    # No proxy supplied by an external caller may bypass Caddy.
    values = request.headers.getlist("x-dictapp-client-ip")
    if len(values) > 1:
        raise HTTPException(status_code=400, detail="Invalid client address")
    raw = values[0] if values else (request.client.host if request.client else "")
    try:
        if "%" in raw:
            raise ValueError("Scoped address not allowed")
        address = ip_address(raw.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid client address") from None
    # IPv4-mapped IPv6 and IPv4 must use the same bucket.
    return str(getattr(address, "ipv4_mapped", None) or address)


ai_limiter = AILimiter()


async def limit_ai_request(request: Request):
    client_ip = get_ai_client_ip(request)
    ai_limiter.reserve(client_ip)
    try:
        yield
    finally:
        # Synchronous cleanup also runs during task cancellation.
        ai_limiter.release(client_ip)
