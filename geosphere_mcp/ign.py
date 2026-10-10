"""Accès générique aux ~1 000 couches vecteur du WFS Géoplateforme (BD TOPO, RPG, ADMIN EXPRESS, OCS GE...)."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

from .batiments import WFS, wfs
from .http import SourceError, client
from .util import arrondir_coords, couche_geojson, fr, repartition, reponse, stats

SRC_IGN = {"producteur": "IGN et partenaires, WFS Géoplateforme", "url": "https://geoservices.ign.fr/services-geoplateforme-diffusion"}
_couches: tuple[float, list[dict]] | None = None
_NS = {"wfs": "http://www.opengis.net/wfs/2.0"}


async def _catalogue() -> list[dict]:
    global _couches
    if _couches and _couches[0] > time.time():
        return _couches[1]
    try:
        r = await client().get(WFS, params={"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"})
        r.raise_for_status()
    except Exception as e:
        raise SourceError(f"WFS IGN injoignable ({type(e).__name__})") from e
    root = ET.fromstring(r.content)
    out = []
    for ft in root.iterfind(".//wfs:FeatureType", _NS):
        out.append({"nom": ft.findtext("wfs:Name", "", _NS), "titre": ft.findtext("wfs:Title", "", _NS)})
    _couches = (time.time() + 86400, out)
    return out


async def lister_couches(recherche: str) -> dict:
    mots = [m for m in re.split(r"\s+", recherche.lower()) if m]
    trouves = [c for c in await _catalogue() if all(m in (c["nom"] + " " + c["titre"]).lower() for m in mots)]
    return reponse(f"Catalogue IGN · {len(trouves)} couche(s) « {recherche} »", SRC_IGN, couches=trouves[:40], tronque=len(trouves) > 40)


async def interroger_couche(couche: str, lon: float, lat: float, rayon_m: float, champs: list[str] | None) -> dict:
    feats, total = await wfs(couche, lon, lat, rayon_m, max_objets=2000)
    if not feats:
        return reponse(f"WFS IGN · {couche} · aucun objet", SRC_IGN, nb_objets=0)
    props = [f["properties"] for f in feats]
    cles = champs or list(props[0].keys())
    resume = {}
    for k in cles:
        vals = [p.get(k) for p in props]
        num = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if num and len(num) >= len([v for v in vals if v is not None]) * 0.9:
            resume[k] = stats(num)
        else:
            distincts = {str(v) for v in vals if v not in (None, "")}
            if not distincts or (len(distincts) > 50 and not champs):
                continue  # identifiants et champs vides : bruit pour le modèle
            resume[k] = repartition((str(v) if v not in (None, "") else None for v in vals), top=8)
    return reponse(
        f"WFS IGN · {couche} · {fr(len(props))} objets",
        SRC_IGN,
        nb_objets=len(props),
        tronque=bool(isinstance(total, int) and total > len(props)),
        attributs=resume,
        exemple=props[0] if len(props) <= 3 else None,
    )


async def entites(couche: str, lon: float, lat: float, rayon_m: float, champs: list[str] | None, champ_suggere: str | None,
                  limite: int, offset: int) -> dict:
    feats, total = await wfs(couche, lon, lat, rayon_m, max_objets=limite, debut=offset)
    if not feats:
        raise SourceError(f"Aucun objet {couche} dans un carré de {fr(2 * rayon_m)} m")
    garder = set(champs) if champs else None
    out = [{"type": "Feature", "geometry": arrondir_coords(f.get("geometry")),
            "properties": {k: v for k, v in f["properties"].items() if (k in garder if garder else not isinstance(v, (dict, list)))}}
           for f in feats]
    total = total if isinstance(total, int) and total >= offset + len(out) else offset + len(out) + (1 if len(out) == limite else 0)
    props = [f["properties"] for f in out]
    resume = {}
    for k in champs or []:
        num = [p.get(k) for p in props if isinstance(p.get(k), (int, float)) and not isinstance(p.get(k), bool)]
        if num:
            resume[k] = stats(num)
    nom = couche.split(":")[-1].replace("_", " ")
    return couche_geojson(nom, out, source={**SRC_IGN, "couche": couche}, offset=offset, limite=limite, total=total,
                          champ_suggere=champ_suggere, resume=resume or None)
