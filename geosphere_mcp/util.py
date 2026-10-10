"""Petits utilitaires : format des nombres, statistiques, enveloppe de réponse commune."""

from __future__ import annotations

import math
from collections import Counter
from statistics import median
from typing import Any, Iterable

LICENCE = "Licence Ouverte 2.0 (Etalab)"


def fr(n: float | int | None, dec: int = 0) -> str:
    """1240.5 -> '1 240' ; espace fine insécable comme séparateur de milliers."""
    if n is None:
        return "?"
    s = f"{n:,.{dec}f}".replace(",", " ").replace(".", ",")
    return s


def reponse(etape: str, source: dict, **donnees: Any) -> dict:
    """Enveloppe commune à tous les outils.

    `etape` est la ligne affichée dans le fil d'étapes du chatbot (ex. « Requête DVF · 1 240 ventes »).
    `source` porte producteur, millésime, licence et url pour que Claude cite ses chiffres.
    """
    return {"etape": etape, **donnees, "source": {"licence": LICENCE, **source}}


def repartition(valeurs: Iterable[Any], top: int = 8) -> list[dict]:
    """Compte par modalité, en tableau trié, avec part en % ; regroupe la traîne en « Autres »."""
    c = Counter(v if v not in (None, "") else "Non renseigné" for v in valeurs)
    total = sum(c.values()) or 1
    items = c.most_common()
    out = [{"modalite": k, "n": n, "part_pct": round(100 * n / total, 1)} for k, n in items[:top]]
    reste = sum(n for _, n in items[top:])
    if reste:
        out.append({"modalite": "Autres", "n": reste, "part_pct": round(100 * reste / total, 1)})
    return out


def stats(valeurs: Iterable[float | None]) -> dict | None:
    v = sorted(x for x in valeurs if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not v:
        return None
    q = lambda p: v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))]  # noqa: E731
    return {"n": len(v), "min": v[0], "q1": q(0.25), "mediane": median(v), "q3": q(0.75), "max": v[-1]}


def point_geojson(lon: float, lat: float) -> str:
    return f'{{"type":"Point","coordinates":[{lon},{lat}]}}'


def bbox_autour(lon: float, lat: float, rayon_m: float) -> tuple[float, float, float, float]:
    """Emprise WGS84 (lon_min, lat_min, lon_max, lat_max) d'un carré de demi-côté rayon_m."""
    dlat = rayon_m / 111_320
    dlon = rayon_m / (111_320 * math.cos(math.radians(lat)))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def departement_de(code_insee: str) -> str:
    return code_insee[:3] if code_insee.startswith("97") else code_insee[:2]


def arrondir_coords(geom: dict | None, dec: int = 6) -> dict | None:
    """Coordonnées à 6 décimales (~10 cm) : allège nettement le GeoJSON."""
    if not geom or "coordinates" not in geom:
        return geom

    def r(c):
        return round(c, dec) if isinstance(c, (int, float)) else [r(x) for x in c]

    return {**geom, "coordinates": r(geom["coordinates"])}


def couche_geojson(nom: str, features: list[dict], *, source: dict, offset: int = 0, limite: int = 2000, total: int | None = None,
                   champ_suggere: str | None = None, resume: dict | None = None, **extra: Any) -> dict:
    """Réponse « couche » : un résumé court pour le modèle, et le GeoJSON pour la carte.

    Contrat avec l'artifact : la clé `geojson` est chargée comme couche puis RETIRÉE avant de donner
    le résultat au modèle, qui ne lit que etape, couche, pagination et resume.
    Si `total` est absent, `features` est la liste complète et on découpe la page ici.
    """
    if total is None:
        total, features = len(features), features[offset:offset + limite]
    suivant = offset + len(features) if offset + len(features) < total else None
    geom = next((f["geometry"]["type"] for f in features if f.get("geometry")), None)
    return reponse(
        f"Couche · {nom} · {fr(len(features))} entités" + (f" sur {fr(total)}" if suivant is not None or offset else ""),
        source,
        couche={"nom": nom, "geometrie": geom, "nb_entites": len(features), "champ_suggere": champ_suggere,
                "champs": list(features[0]["properties"]) if features else []},
        pagination={"total": total, "offset": offset, "renvoyees": len(features), "offset_suivant": suivant},
        **({"resume": resume} if resume else {}),
        **extra,
        geojson={"type": "FeatureCollection", "features": features},
    )
