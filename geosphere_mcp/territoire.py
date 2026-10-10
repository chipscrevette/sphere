"""Territoires et adresses : API Découpage administratif (geo.api.gouv.fr) et géocodage Géoplateforme (BAN)."""

from __future__ import annotations

import re

from .http import SourceError, get_json
from .util import fr, reponse

GEO = "https://geo.api.gouv.fr"
GEOCODAGE = "https://data.geopf.fr/geocodage"
CHAMPS_COMMUNE = "code,nom,population,surface,centre,codesPostaux,codeEpci,epci,departement,region"

SRC_GEO = {"producteur": "IGN / INSEE (COG) via API Découpage administratif", "url": GEO}
SRC_BAN = {"producteur": "Base Adresse Nationale (IGN / DINUM) via Géoplateforme", "url": GEOCODAGE}


def _commune(c: dict) -> dict:
    surf_km2 = (c.get("surface") or 0) / 100  # l'API donne des hectares
    pop = c.get("population")
    return {
        "type": "commune",
        "code_insee": c["code"],
        "nom": c["nom"],
        "population": pop,
        "surface_km2": round(surf_km2, 2) if surf_km2 else None,
        "densite_hab_km2": round(pop / surf_km2) if pop and surf_km2 else None,
        "codes_postaux": c.get("codesPostaux"),
        "epci": (c.get("epci") or {}).get("nom") and {"code": c["epci"]["code"], "nom": c["epci"]["nom"]},
        "departement": c.get("departement"),
        "region": c.get("region"),
        "centre_lonlat": (c.get("centre") or {}).get("coordinates"),
    }


async def commune_au_point(lon: float, lat: float) -> dict:
    res = await get_json(f"{GEO}/communes", {"lon": lon, "lat": lat, "fields": CHAMPS_COMMUNE}, source="API Découpage administratif")
    if not res:
        raise SourceError("Aucune commune française à ce point")
    return _commune(res[0])


async def resoudre_territoire(requete: str) -> dict:
    q = requete.strip()
    src = "API Découpage administratif"
    if re.fullmatch(r"\d{9}", q):  # SIREN d'EPCI
        e = await get_json(f"{GEO}/epcis/{q}",{"fields": "code,nom,population,codesDepartements,codesRegions"}, source=src)
        membres = await get_json(f"{GEO}/epcis/{q}/communes", {"fields": "code,nom,population"}, source=src)
        membres.sort(key=lambda c: -(c.get("population") or 0))
        return reponse(
            f"Territoire · EPCI {e['nom']} · {len(membres)} communes",
            SRC_GEO,
            resultats=[{
                "type": "epci", "code": e["code"], "nom": e["nom"], "population": e.get("population"),
                "departements": e.get("codesDepartements"), "nb_communes": len(membres),
                "communes_principales": [f"{c['nom']} ({c['code']}, {fr(c.get('population'))} hab.)" for c in membres[:10]],
            }],
        )
    if re.fullmatch(r"(\d{2}|2[AB]|97\d)", q, re.I):  # département
        d = await get_json(f"{GEO}/departements/{q.upper()}", {"fields": "code,nom,codeRegion"}, source=src)
        return reponse(f"Territoire · département {d['nom']}", SRC_GEO, resultats=[{"type": "departement", **d}])
    if re.fullmatch(r"(\d{5}|2[AB]\d{3})", q, re.I):  # code INSEE puis code postal
        try:
            c = await get_json(f"{GEO}/communes/{q.upper()}", {"fields": CHAMPS_COMMUNE}, source=src)
            return reponse(f"Territoire · {c['nom']} ({c['code']})", SRC_GEO, resultats=[_commune(c)])
        except SourceError:
            cs = await get_json(f"{GEO}/communes", {"codePostal": q, "fields": CHAMPS_COMMUNE}, source=src)
            return reponse(f"Territoire · code postal {q} · {len(cs)} commune(s)", SRC_GEO, resultats=[_commune(c) for c in cs])
    cs = await get_json(f"{GEO}/communes", {"nom": q, "fields": CHAMPS_COMMUNE, "boost": "population", "limit": 5}, source=src)
    if not cs:
        raise SourceError(f"Aucune commune ne correspond à « {q} ». Essayer un code INSEE, un code postal ou geocoder().")
    return reponse(f"Territoire · {cs[0]['nom']} ({cs[0]['code']})", SRC_GEO, resultats=[_commune(c) for c in cs])


def _adresse(f: dict) -> dict:
    p = f["properties"]
    lon, lat = f["geometry"]["coordinates"]
    return {
        "label": p.get("label"), "type": p.get("type"), "score": round(p.get("score", 0), 2),
        "lon": lon, "lat": lat, "x_l93": p.get("x"), "y_l93": p.get("y"),
        "code_insee": p.get("citycode"), "code_postal": p.get("postcode"), "commune": p.get("city"),
        "contexte": p.get("context"), "id_ban": p.get("id"),
        **({"distance_m": p["distance"]} if "distance" in p else {}),
    }


async def geocoder(adresse: str | None, lon: float | None, lat: float | None, limite: int) -> dict:
    if adresse:
        r = await get_json(f"{GEOCODAGE}/search", {"q": adresse, "limit": limite}, source="Géocodage BAN")
        feats = r.get("features", [])
        if not feats:
            raise SourceError(f"Adresse introuvable : « {adresse} »")
        return reponse(f"Géocodage BAN · {feats[0]['properties'].get('label')}", SRC_BAN, resultats=[_adresse(f) for f in feats])
    if lon is None or lat is None:
        raise SourceError("Donner une adresse, ou lon et lat pour le géocodage inverse")
    r = await get_json(f"{GEOCODAGE}/reverse", {"lon": lon, "lat": lat, "limit": limite}, source="Géocodage BAN")
    feats = r.get("features", [])
    if not feats:
        raise SourceError("Aucune adresse à proximité de ce point")
    return reponse(f"Géocodage inverse · {feats[0]['properties'].get('label')}", SRC_BAN, resultats=[_adresse(f) for f in feats])
