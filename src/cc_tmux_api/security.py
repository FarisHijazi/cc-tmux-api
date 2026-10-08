"""Same-origin controls with optional account-independent device pairing."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import JSONResponse

COOKIE = "hq_device"
MAX_AGE = 30 * 24 * 60 * 60


class Security:
    def __init__(self, token: str, public_url: str):
        if token and len(token) < 32:
            raise ValueError("CC_TMUX_TOKEN must contain at least 32 characters")
        self.token = token
        self.public_url = public_url.rstrip("/")
        parsed = urlsplit(self.public_url)
        if self.public_url and (
            parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
        ):
            raise ValueError("CC_TMUX_PUBLIC_URL must be an HTTPS origin")
        self.hosts = {"localhost", "127.0.0.1", "::1"}
        self.hosts.update(name.strip() for name in os.environ.get("CC_TMUX_ALLOWED_HOSTS", "").split(",") if name.strip())
        if parsed.hostname:
            self.hosts.add(parsed.hostname)
        self.attempts: dict[str, deque] = defaultdict(deque)

    def cookie(self) -> str:
        payload = f"{int(time.time())}.{secrets.token_hex(16)}"
        signature = hmac.new(self.token.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def authenticated(self, request: Request) -> bool:
        if not self.token:
            return True
        bearer = request.headers.get("authorization", "")
        if bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:], self.token):
            return True
        cookie = request.cookies.get(COOKIE, "")
        if len(cookie) > 200:
            return False
        try:
            issued, nonce, signature = cookie.split(".")
            age = time.time() - int(issued)
        except ValueError:
            return False
        expected = hmac.new(
            self.token.encode(), f"{issued}.{nonce}".encode(), hashlib.sha256
        ).hexdigest()
        return 0 <= age <= MAX_AGE and hmac.compare_digest(signature, expected)

    def can_attempt(self, address: str) -> bool:
        now = time.monotonic()
        # Bound memory even if many clients try pairing.
        if len(self.attempts) > 1000:
            self.attempts.clear()
        attempts = self.attempts[address]
        while attempts and attempts[0] < now - 60:
            attempts.popleft()
        if len(attempts) >= 10:
            return False
        attempts.append(now)
        return True

    async def middleware(self, request: Request, call_next):
        if request.url.hostname not in self.hosts:
            return JSONResponse({"detail": "Unrecognized server address"}, status_code=400)
        api = request.url.path.startswith("/api/")
        if api:
            origin = request.headers.get("origin")
            expected = {self.public_url, f"{request.url.scheme}://{request.url.netloc}"}
            if (origin and origin not in expected) or request.headers.get(
                "sec-fetch-site"
            ) == "cross-site":
                return JSONResponse(
                    {"detail": "Cross-site requests are not allowed"}, status_code=403
                )
            public = request.url.path in {"/api/auth", "/api/auth/login"}
            if not public and not self.authenticated(request):
                return JSONResponse({"detail": "Pair this device to continue"}, status_code=401)
            if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
                if not request.headers.get("content-type", "").startswith("application/json"):
                    return JSONResponse({"detail": "JSON content type required"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if api:
            response.headers["Cache-Control"] = "no-store"
        return response
