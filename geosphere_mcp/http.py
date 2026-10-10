"""Client HTTP partagé : timeouts, cache mémoire à durée de vie, erreurs lisibles."""

from __future__ import annotations

import csv
import io
import time
from typing import Any

import httpx

USER_AGENT = "Geosphere-MCP/0.1 (+assistant SIG)"
_TTL = 6 * 3600  # les sources bougent au mieux quotidiennement
_cache: dict[str, tuple[float, Any]] = {}
_client: httpx.AsyncClient | None = None


class SourceError(RuntimeError):
    """Erreur côté source, formulée pour être relayée telle quelle à l'utilisateur."""


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=8.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
    return _client


async def _get(url: str, params: dict | None, source: str, headers: dict | None = None) -> httpx.Response:
    try:
        r = await client().get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise SourceError(f"{source} injoignable ({type(e).__name__})") from e
    if r.status_code == 404:
        raise SourceError(f"{source} : aucune donnée (404)")
    if r.status_code >= 400:
        raise SourceError(f"{source} : erreur {r.status_code} {r.text[:200]}")
    return r


def _key(url: str, params: dict | None) -> str:
    return url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))


async def get_json(url: str, params: dict | None = None, *, source: str, headers: dict | None = None) -> Any:
    k = _key(url, params)
    hit = _cache.get(k)
    if hit and hit[0] > time.time():
        return hit[1]
    data = (await _get(url, params, source, headers)).json()
    _cache[k] = (time.time() + _TTL, data)
    return data


async def get_csv(url: str, *, source: str) -> list[dict[str, str]]:
    k = _key(url, None)
    hit = _cache.get(k)
    if hit and hit[0] > time.time():
        return hit[1]
    text = (await _get(url, None, source)).text
    rows = list(csv.DictReader(io.StringIO(text)))
    _cache[k] = (time.time() + _TTL, rows)
    return rows
