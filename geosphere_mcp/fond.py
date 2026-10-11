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


# ---------- une image par vue (WMS GetMap), comme QGIS : un seul aller-retour au lieu de dizaines de tuiles ----------
IGN_WMS = "https://data.geopf.fr/wms-r/wms"
EOX_WMS = "https://tiles.maps.eox.at/wms"
VUE_MAX = 2048
O = 20037508.342789244
_vues: OrderedDict[str, tuple[str, str]] = OrderedDict()
_VUES_MAX = 300


def _lonlat(x: float, y: float) -> tuple[float, float]:
    return x / O * 180, math.degrees(2 * math.atan(math.exp(y / O * math.pi)) - math.pi / 2)


async def vue(bbox: list[float], largeur: int, hauteur: int, source: str = "auto") -> dict:
    """Image JPEG de l'emprise bbox (EPSG:3857, [xmin, ymin, xmax, ymax]) à la taille demandée."""
    if len(bbox) != 4 or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
        raise ValueError("bbox attendue : [xmin, ymin, xmax, ymax] en EPSG:3857")
    if not (16 <= largeur <= VUE_MAX and 16 <= hauteur <= VUE_MAX):
        raise ValueError(f"taille entre 16 et {VUE_MAX} px")
    lon, lat = _lonlat((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
    src = source if source != "auto" else ("ign" if any(a <= lon <= c and b <= lat <= d for a, b, c, d in FRANCE) else "eox")
    key = f"{src}/{largeur}x{hauteur}/" + ",".join(f"{v:.1f}" for v in bbox)
    if key in _vues:
        _vues.move_to_end(key)
        s, b64 = _vues[key]
        return {"etape": "Fond satellite (cache)", "jpeg_base64": b64, "bbox": bbox, "largeur": largeur, "hauteur": hauteur, "source": [SOURCES[s]]}
    b = ",".join(f"{v:.3f}" for v in bbox)
    if src == "ign":
        url, params = IGN_WMS, {"SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap", "LAYERS": "ORTHOIMAGERY.ORTHOPHOTOS", "STYLES": "",
                                "CRS": "EPSG:3857", "BBOX": b, "WIDTH": largeur, "HEIGHT": hauteur, "FORMAT": "image/jpeg"}
    else:
        url, params = EOX_WMS, {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": "s2cloudless_3857", "STYLES": "",
                                "SRS": "EPSG:3857", "BBOX": b, "WIDTH": largeur, "HEIGHT": hauteur, "FORMAT": "image/jpeg"}
    try:
        r = await client().get(url, params=params, headers={"Accept": "image/jpeg,image/*"})
    except httpx.HTTPError as e:
        return {"etape": f"Fond satellite, {src} injoignable", "erreur": f"{src} injoignable ({type(e).__name__})"}
    if r.status_code >= 400 or not r.headers.get("content-type", "").startswith("image/"):
        if src == "ign" and source == "auto":  # hors couverture de l'ortho : Sentinel-2
            return await vue(bbox, largeur, hauteur, "eox")
        return {"etape": f"Fond satellite, échec {src}", "erreur": f"{src} : réponse {r.status_code} {r.text[:160]}"}
    b64 = base64.b64encode(r.content).decode()
    _vues[key] = (src, b64)
    if len(_vues) > _VUES_MAX:
        _vues.popitem(last=False)
    return {"etape": "Fond satellite", "jpeg_base64": b64, "bbox": bbox, "largeur": largeur, "hauteur": hauteur, "source": [SOURCES[src]]}
