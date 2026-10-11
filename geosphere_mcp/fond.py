"""Fond de carte satellite pour la page : tuiles relayées par le serveur (la page ne peut pas joindre les serveurs de tuiles).

France : orthophotos IGN (BD ORTHO, 20 cm, Licence Ouverte). Ailleurs : Sentinel-2 cloudless 2016 d'EOX (10 m, CC BY 4.0).
Tuiles Web Mercator z/x/y (schéma XYZ, origine en haut à gauche), JPEG 256 px.
"""

from __future__ import annotations

import asyncio
import base64
import math
from collections import OrderedDict

import httpx

from .http import client

IGN = ("https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal"
       "&TILEMATRIXSET=PM&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg")
EOX = "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg"
EOX_ZMAX = 15
MAX_TUILES = 24

# emprises couvertes par l'orthophoto IGN : métropole, Corse, outre-mer (lon_min, lat_min, lon_max, lat_max)
FRANCE = [(-5.3, 41.3, 9.7, 51.2), (-61.9, 15.8, -60.9, 16.6), (-61.3, 14.3, -60.7, 14.95), (-54.7, 2.0, -51.5, 5.9),
          (55.1, -21.5, 55.9, -20.8), (44.9, -13.1, 45.4, -12.5), (-56.5, 46.7, -56.1, 47.2)]

SOURCES = {
    "ign": {"producteur": "IGN, BD ORTHO (orthophotographies)", "licence": "Licence Ouverte 2.0 (Etalab)", "url": "https://geoservices.ign.fr/bdortho"},
    "eox": {"producteur": "Sentinel-2 cloudless 2016 by EOX IT Services GmbH (contient des données Copernicus Sentinel modifiées 2016)",
            "licence": "CC BY 4.0", "url": "https://s2maps.eu"},
}

_cache: OrderedDict[str, tuple[str, str]] = OrderedDict()
_CACHE_MAX = 3000


def centre(z: int, x: int, y: int) -> tuple[float, float]:
    n = 2 ** z
    lon = (x + .5) / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + .5) / n))))
    return lon, lat


def en_france(z: int, x: int, y: int) -> bool:
    lon, lat = centre(z, x, y)
    return any(a <= lon <= c and b <= lat <= d for a, b, c, d in FRANCE)


async def _tuile(z: int, x: int, y: int, source: str) -> dict:
    src = source if source != "auto" else ("ign" if en_france(z, x, y) else "eox")
    if src == "eox" and z > EOX_ZMAX:
        return {"z": z, "x": x, "y": y, "erreur": f"Sentinel-2 s'arrête au zoom {EOX_ZMAX}"}
    key = f"{src}/{z}/{x}/{y}"
    if key in _cache:
        _cache.move_to_end(key)
        s, b64 = _cache[key]
        return {"z": z, "x": x, "y": y, "source": s, "jpeg_base64": b64}
    url = (IGN if src == "ign" else EOX).format(z=z, x=x, y=y)
    try:
        r = await client().get(url, headers={"Accept": "image/jpeg,image/*"})
    except httpx.HTTPError as e:
        return {"z": z, "x": x, "y": y, "erreur": f"{src} injoignable ({type(e).__name__})"}
    if r.status_code >= 400 or not r.headers.get("content-type", "").startswith("image/"):
        if src == "ign" and source == "auto" and z <= EOX_ZMAX:  # trou dans l'ortho : on bascule sur Sentinel-2
            return await _tuile(z, x, y, "eox")
        return {"z": z, "x": x, "y": y, "erreur": f"{src} : réponse {r.status_code}"}
    b64 = base64.b64encode(r.content).decode()
    _cache[key] = (src, b64)
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return {"z": z, "x": x, "y": y, "source": src, "jpeg_base64": b64}


async def fond(tuiles: list[str], source: str = "auto") -> dict:
    zxy = []
    for t in tuiles[:MAX_TUILES]:
        z, x, y = (int(v) for v in t.split("/"))
        if not (0 <= z <= 20 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
            raise ValueError(f"tuile hors grille : {t}")
        zxy.append((z, x, y))
    res = await asyncio.gather(*(_tuile(z, x, y, source) for z, x, y in zxy))
    ok = [r for r in res if "jpeg_base64" in r]
    used = sorted({r["source"] for r in ok})
    return {"etape": f"Fond satellite, {len(ok)} tuile{'s' if len(ok) > 1 else ''}", "tuiles": res,
            "ignorees": max(0, len(tuiles) - MAX_TUILES), "source": [SOURCES[s] for s in used]}
