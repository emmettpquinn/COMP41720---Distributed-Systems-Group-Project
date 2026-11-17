"""Same-origin development gateway with explicit routes and bounded upstream calls."""

import math
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).resolve().parents[1] / "static"
ROUTES = {
    "catalogue": [("GET", r"/events"), ("GET", r"/events/[^/]+")],
    "booking": [("POST", r"/holds")],
    "discovery": [("GET", r"/search"), ("GET", r"/events/[^/]+/similar")],
    "ticketing": [("GET", r"/tickets/[^/]+")],
}


@dataclass(frozen=True)
class Settings:
    catalogue_url: str = "http://catalogue:8000"
    booking_url: str = "http://booking:8000"
    ticketing_url: str = "http://ticketing:8000"
    timeout_seconds: float = 3
    holds_enabled: bool = False
    discovery_url: str = "http://discovery:8000"
    search_enabled: bool = False

    def __post_init__(self):
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 30:
            raise ValueError("Upstream timeout must be finite, positive and at most 30 seconds")
        for url in (self.catalogue_url, self.booking_url, self.ticketing_url, self.discovery_url):
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path not in {"", "/"} or parts.query or parts.fragment or parts.username:
                raise ValueError("Upstreams must be HTTP origins without credentials or paths")

    @classmethod
    def from_env(cls):
        enabled = os.getenv("BOOKING_HOLDS_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise ValueError("BOOKING_HOLDS_ENABLED must be true or false")
        search = os.getenv("DISCOVERY_SEARCH_ENABLED", "false").lower()
        if search not in {"true", "false"}:
            raise ValueError("DISCOVERY_SEARCH_ENABLED must be true or false")
        return cls(
            catalogue_url=os.getenv("CATALOGUE_URL", "http://catalogue:8000"),
            booking_url=os.getenv("BOOKING_URL", "http://booking:8000"),
            ticketing_url=os.getenv("TICKETING_URL", "http://ticketing:8000"),
            timeout_seconds=float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "3")),
            holds_enabled=enabled == "true",
            discovery_url=os.getenv("DISCOVERY_URL", "http://discovery:8000"),
            search_enabled=search == "true",
        )


def create_app(settings=None, transport=None):
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_seconds), transport=transport,
            follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        ) as client:
            app.state.upstream = client
            yield

    app = FastAPI(title="StageDoor gateway", lifespan=lifespan)

    @app.middleware("http")
    async def browser_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health():
        return {"service": "gateway", "status": "ok"}

    @app.get("/api/capabilities")
    async def capabilities():
        return {"holds": settings.holds_enabled, "checkout": False, "search": settings.search_enabled}

    @app.api_route("/api/{service}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(service: str, path: str, request: Request):
        upstream_path = "/" + path
        if any(part in {".", ".."} for part in path.split("/")) or "\\" in path or "%" in path:
            raise HTTPException(400, "Invalid path")
        matches = [method for method, pattern in ROUTES.get(service, []) if re.fullmatch(pattern, upstream_path)]
        if not matches:
            raise HTTPException(404, "Route is unavailable")
        if request.method not in matches:
            raise HTTPException(405, "Method is unavailable")
        if service == "booking" and not settings.holds_enabled:
            raise HTTPException(503, "Seat reservations are unavailable in this stack")
        if service == "discovery" and not settings.search_enabled:
            raise HTTPException(503, "Search is unavailable in this stack")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Request is too large")
        headers = {"Accept": "application/json"}
        if body:
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                raise HTTPException(415, "Use application/json")
            headers["Content-Type"] = "application/json"
        origin = getattr(settings, service + "_url").rstrip("/")
        try:
            # Do not retry state-changing requests: a timeout can have an unknown outcome.
            response = await app.state.upstream.request(
                request.method, origin + upstream_path, content=bytes(body),
                params=request.query_params, headers=headers,
            )
        except httpx.TimeoutException as error:
            detail = "Service timed out; reservation outcome may be unknown" if service == "booking" and request.method == "POST" else "Service timed out; try again later"
            raise HTTPException(504, detail) from error
        except httpx.RequestError as error:
            raise HTTPException(503, "Service is temporarily unavailable") from error
        if 300 <= response.status_code < 400:
            raise HTTPException(502, "Unexpected service redirect")
        if "application/json" not in response.headers.get("content-type", ""):
            raise HTTPException(502, "Unexpected service response")
        return Response(response.content, status_code=response.status_code, media_type="application/json")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app


app = create_app()
