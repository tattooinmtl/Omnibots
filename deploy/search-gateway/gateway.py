"""Search Gateway: a private, keyed JSON search API in front of SearXNG.

Every agent (OmniBots, GWN-Agent, ...) gets its own API key. Keys are stored
hashed; a request without a valid key gets 401. Per-key rate limit, a result
cache (fewer hits on the upstream engines = fewer CAPTCHAs/suspensions), and a
usage log per key.

  GET /search?q=...&category=general|it|science|news&lang=en&page=1&limit=10
      Authorization: Bearer <key>     (or X-API-Key: <key>)
  GET /health                          engines that answer right now (key required)
  GET /usage                           your key's recent searches (key required)

Admin (on the VPS):
  docker exec search-gateway python gateway.py keys add <name>     # prints the key ONCE
  docker exec search-gateway python gateway.py keys list
  docker exec search-gateway python gateway.py keys revoke <name>
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import threading
import time
from collections import OrderedDict, deque
from contextlib import closing

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

SEARXNG = os.environ.get("SEARXNG_URL", "http://searxng-fc:8080").rstrip("/")
DATA = os.environ.get("DATA_DIR", "/data")
RATE = int(os.environ.get("RATE_PER_MIN", "30"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))
CACHE_MAX = int(os.environ.get("CACHE_MAX", "2000"))
CATEGORIES = {"general", "it", "science", "news", "images", "videos", "map", "files", "social media"}
DB = os.path.join(DATA, "gateway.sqlite")

_db_lock = threading.Lock()


def db() -> sqlite3.Connection:
    os.makedirs(DATA, exist_ok=True)
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS keys (name TEXT PRIMARY KEY, key_hash TEXT NOT NULL,
                   created_at REAL NOT NULL, revoked_at REAL, rate_per_min INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, ts REAL NOT NULL, key_name TEXT NOT NULL,
                   q TEXT, category TEXT, results INTEGER, cached INTEGER, ms INTEGER, status INTEGER, client TEXT)""")
    return con


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_owner(key: str | None) -> tuple[str, int] | None:
    if not key:
        return None
    h = _hash(key.strip())
    with _db_lock, closing(db()) as con:
        for name, kh, rate in con.execute("SELECT name, key_hash, rate_per_min FROM keys WHERE revoked_at IS NULL"):
            if hmac.compare_digest(kh, h):
                return name, int(rate or RATE)
    return None


def log_usage(**row) -> None:
    with _db_lock, closing(db()) as con:
        con.execute("INSERT INTO usage (ts, key_name, q, category, results, cached, ms, status, client) VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), row["key"], row.get("q"), row.get("category"), row.get("results"), int(row.get("cached", 0)),
                     row.get("ms"), row.get("status"), row.get("client")))
        con.commit()


# ── rate limit (sliding minute per key) and cache ──────────────────────────
_hits: dict[str, deque] = {}
_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_mem_lock = threading.Lock()


def allow(name: str, rate: int) -> bool:
    now = time.monotonic()
    with _mem_lock:
        q = _hits.setdefault(name, deque())
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= rate:
            return False
        q.append(now)
        return True


def cache_get(k: tuple) -> dict | None:
    with _mem_lock:
        v = _cache.get(k)
        if not v:
            return None
        if time.time() - v[0] > CACHE_TTL:
            _cache.pop(k, None)
            return None
        _cache.move_to_end(k)
        return v[1]


def cache_put(k: tuple, data: dict) -> None:
    with _mem_lock:
        _cache[k] = (time.time(), data)
        _cache.move_to_end(k)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


# ── API ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="Search Gateway", docs_url=None, redoc_url=None, openapi_url=None)
client = httpx.AsyncClient(timeout=httpx.Timeout(25.0))


def _auth(authorization: str | None, x_api_key: str | None) -> tuple[str, int]:
    key = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    who = key_owner(key)
    if not who:
        raise HTTPException(401, "missing or invalid API key")
    return who


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


@app.get("/search")
async def search(request: Request, q: str = Query(..., min_length=1, max_length=400), category: str = "general",
                 lang: str = "en", page: int = Query(1, ge=1, le=5), limit: int = Query(10, ge=1, le=30),
                 authorization: str | None = Header(None), x_api_key: str | None = Header(None)):
    name, rate = _auth(authorization, x_api_key)
    if category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {sorted(CATEGORIES)}")
    if not allow(name, rate):
        log_usage(key=name, q=q, category=category, results=0, ms=0, status=429, client=_client_ip(request))
        raise HTTPException(429, f"rate limit: {rate} searches per minute for this key")
    ck = (q.strip().lower(), category, lang, page)
    t0 = time.monotonic()
    data = cache_get(ck)
    cached = data is not None
    if data is None:
        try:
            r = await client.get(f"{SEARXNG}/search", params={"q": q, "format": "json", "categories": category,
                                                              "language": lang, "pageno": page})
        except httpx.HTTPError as exc:
            log_usage(key=name, q=q, category=category, results=0, ms=int((time.monotonic() - t0) * 1000), status=502, client=_client_ip(request))
            raise HTTPException(502, f"search backend unreachable: {type(exc).__name__}")
        if r.status_code != 200:
            raise HTTPException(502, f"search backend HTTP {r.status_code}")
        raw = r.json()
        data = {
            "query": q, "category": category,
            "results": [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": (x.get("content") or "")[:500],
                         "engines": x.get("engines", []), "published": x.get("publishedDate")} for x in raw.get("results", [])],
            "answers": [a if isinstance(a, str) else a.get("answer", "") for a in raw.get("answers", [])][:3],
            "infobox": ({"title": raw["infoboxes"][0].get("infobox"), "text": (raw["infoboxes"][0].get("content") or "")[:800],
                         "url": raw["infoboxes"][0].get("id")} if raw.get("infoboxes") else None),
            "suggestions": raw.get("suggestions", [])[:5],
            "unresponsive_engines": [f"{e[0]}: {e[1]}" for e in raw.get("unresponsive_engines", [])],
        }
        if data["results"]:
            cache_put(ck, data)
    out = dict(data, results=data["results"][:limit], cached=cached)
    log_usage(key=name, q=q, category=category, results=len(out["results"]), cached=cached,
              ms=int((time.monotonic() - t0) * 1000), status=200, client=_client_ip(request))
    return out


@app.get("/health")
async def health(authorization: str | None = Header(None), x_api_key: str | None = Header(None)):
    _auth(authorization, x_api_key)
    try:
        r = await client.get(f"{SEARXNG}/search", params={"q": "wikipedia", "format": "json"})
        raw = r.json()
        working = sorted({e for x in raw.get("results", []) for e in x.get("engines", [])})
        return {"ok": bool(working), "working_engines": working,
                "unresponsive_engines": [f"{e[0]}: {e[1]}" for e in raw.get("unresponsive_engines", [])],
                "cache_entries": len(_cache)}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=502)


@app.get("/usage")
async def usage(limit: int = Query(50, ge=1, le=500), authorization: str | None = Header(None), x_api_key: str | None = Header(None)):
    name, rate = _auth(authorization, x_api_key)
    with _db_lock, closing(db()) as con:
        rows = con.execute("SELECT ts, q, category, results, cached, ms, status FROM usage WHERE key_name=? ORDER BY id DESC LIMIT ?",
                           (name, limit)).fetchall()
        day = con.execute("SELECT COUNT(*) FROM usage WHERE key_name=? AND ts > ?", (name, time.time() - 86400)).fetchone()[0]
    return {"key": name, "rate_per_min": rate, "searches_last_24h": day,
            "recent": [dict(zip(("ts", "q", "category", "results", "cached", "ms", "status"), r)) for r in rows]}


# ── admin CLI ───────────────────────────────────────────────────────────────
def cli(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "keys":
        print(__doc__)
        return 2
    cmd = argv[1]
    with closing(db()) as con:
        if cmd == "add" and len(argv) >= 3:
            name = argv[2]
            key = "sg_" + secrets.token_urlsafe(32)
            rate = int(argv[3]) if len(argv) > 3 else None
            con.execute("INSERT OR REPLACE INTO keys (name, key_hash, created_at, revoked_at, rate_per_min) VALUES (?,?,?,NULL,?)",
                        (name, _hash(key), time.time(), rate))
            con.commit()
            print(key)          # shown once; only the hash is stored
            return 0
        if cmd == "list":
            for name, created, revoked, rate in con.execute("SELECT name, created_at, revoked_at, rate_per_min FROM keys ORDER BY name"):
                state = "revoked" if revoked else "active"
                print(f"{name:20} {state:8} created {time.strftime('%Y-%m-%d', time.localtime(created))}  rate {rate or RATE}/min")
            return 0
        if cmd == "revoke" and len(argv) >= 3:
            n = con.execute("UPDATE keys SET revoked_at=? WHERE name=? AND revoked_at IS NULL", (time.time(), argv[2])).rowcount
            con.commit()
            print("revoked" if n else "no active key with that name")
            return 0 if n else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
