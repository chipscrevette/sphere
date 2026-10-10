"""Bâti : BD TOPO (WFS Géoplateforme), BDNB (CSTB, API open data) et DPE logements (ADEME)."""

from __future__ import annotations

import asyncio

from .http import SourceError, get_json
from .util import bbox_autour, fr, repartition, reponse, stats

WFS = "https://data.geopf.fr/wfs/ows"
BDNB = "https://api.bdnb.io/v1/bdnb/donnees"
ADEME = "https://data.ademe.fr/data-fair/api/v1/datasets/dpe03existant"
SRC_BDTOPO = {"producteur": "IGN, BD TOPO via WFS Géoplateforme", "url": "https://geoservices.ign.fr/bdtopo"}
SRC_BDNB = {"producteur": "CSTB, Base de données nationale des bâtiments (open data)", "url": "https://bdnb.io"}
SRC_DPE = {"producteur": "ADEME, DPE logements existants depuis juillet 2021", "url": "https://data.ademe.fr/datasets/dpe03existant"}

MAX_REQUETES_BDNB = 30

CHAMPS_BDNB = ",".join([
    "batiment_groupe_id", "libelle_adr_principale_ban", "l_parcelle_id", "annee_construction", "usage_principal_bdnb_open",
    "nb_log", "nb_niveau", "hauteur_mean", "surface_emprise_sol", "mat_mur_txt", "mat_toit_txt", "classe_bilan_dpe",
    "type_energie_chauffage", "alea_argile", "l_denomination_proprietaire", "quartier_prioritaire",
    "distance_monument_historique", "code_iris",
])


def _periode(annee: int | None) -> str:
    if not annee:
        return "Inconnue"
    for borne, lib in ((1919, "Avant 1919"), (1946, "1919–1945"), (1971, "1946–1970"), (1991, "1971–1990"), (2006, "1991–2005"), (2013, "2006–2012")):
        if annee < borne:
            return lib
    return "2013 et après"


async def wfs(couche: str, lon: float, lat: float, rayon_m: float, max_objets: int = 5000, debut: int = 0) -> tuple[list[dict], int | None]:
    x0, y0, x1, y1 = bbox_autour(lon, lat, rayon_m)
    fc = await get_json(WFS, {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": couche,
        "outputFormat": "application/json", "srsName": "EPSG:4326", "count": max_objets,
        "bbox": f"{y0},{x0},{y1},{x1},urn:ogc:def:crs:EPSG::4326",
        **({"startIndex": debut} if debut else {}),
    }, source=f"WFS IGN {couche}")
    return fc.get("features", []), fc.get("numberMatched")


async def batiments_bdtopo(lon: float, lat: float, rayon_m: float) -> dict:
    feats, total = await wfs("BDTOPO_V3:batiment", lon, lat, rayon_m)
    if not feats:
        raise SourceError("Aucun bâtiment BD TOPO dans ce périmètre")
    p = [f["properties"] for f in feats]
    logements = [x.get("nombre_de_logements") for x in p if x.get("nombre_de_logements")]
    return reponse(
        f"BD TOPO · {fr(len(p))} bâtiments · carré de {fr(2 * rayon_m)} m",
        SRC_BDTOPO,
        nb_batiments=len(p),
        tronque=bool(total and isinstance(total, int) and total > len(p)),
        usage=repartition(x.get("usage_1") for x in p),
        nature=repartition((x.get("nature") for x in p), top=5),
        hauteur_m=stats(x.get("hauteur") for x in p),
        nombre_etages=stats(x.get("nombre_d_etages") for x in p),
        logements_total=sum(logements),
        materiaux_murs=repartition((x.get("materiaux_des_murs") for x in p), top=5),
    )


async def batiments_bdnb(code_insee: str | None, parcelle: str | None, max_batiments: int) -> dict:
    if parcelle:
        rel = await get_json(f"{BDNB}/rel_batiment_groupe_parcelle", {"parcelle_id": f"eq.{parcelle}"}, source="BDNB")
        ids = [r["batiment_groupe_id"] for r in rel]
        if not ids:
            raise SourceError(f"Aucun bâtiment BDNB sur la parcelle {parcelle}")
        rows = await get_json(f"{BDNB}/batiment_groupe_complet", {"batiment_groupe_id": f"in.({','.join(ids)})", "select": CHAMPS_BDNB}, source="BDNB")
        return reponse(f"BDNB · {len(rows)} bâtiment(s) sur {parcelle}", SRC_BDNB, batiments=[{k: v for k, v in r.items() if v not in (None, [], "")} for r in rows])
    if not code_insee:
        raise SourceError("Donner un code INSEE ou un identifiant de parcelle (idu)")
    # Sans clé, l'API open data plafonne chaque réponse (10 lignes constatées le 2026-10-09) :
    # on pagine sur la taille réellement renvoyée, dans la limite de MAX_REQUETES_BDNB appels.
    rows: list[dict] = []
    page = min(max_batiments, 5000)
    complet = False
    for _ in range(MAX_REQUETES_BDNB):
        lot = await get_json(f"{BDNB}/batiment_groupe_complet", {
            "code_commune_insee": f"eq.{code_insee}", "select": CHAMPS_BDNB, "limit": page, "offset": len(rows),
            "order": "batiment_groupe_id",
        }, source="BDNB")
        rows += lot
        if not lot or len(lot) < min(page, 10):
            complet = True
            break
        if len(rows) >= max_batiments:
            break
    if not rows:
        raise SourceError(f"Aucun bâtiment BDNB pour {code_insee}")
    log = [r for r in rows if (r.get("nb_log") or 0) > 0]
    return reponse(
        f"BDNB · {fr(len(rows))} bâtiments · {code_insee}",
        {**SRC_BDNB, "millesime": "BDNB open data, dernier millésime publié"},
        nb_batiments=len(rows),
        echantillon=not complet,
        **({} if complet else {"avertissement": (
            f"Échantillon de {len(rows)} bâtiments (premiers identifiants, non aléatoire) : l'API BDNB sans clé limite le volume. "
            "Pour la commune entière, préférer dpe_logements et batiments_autour, ou attendre l'ingestion PostGIS.")}),
        logements_total=sum(r["nb_log"] for r in log),
        usage=repartition(r.get("usage_principal_bdnb_open") for r in rows),
        periode_construction=repartition((_periode(r.get("annee_construction")) for r in rows), top=10),
        etiquette_dpe_batiments=repartition((r.get("classe_bilan_dpe") for r in log), top=10),
        energie_chauffage=repartition((r.get("type_energie_chauffage") for r in log), top=6),
        alea_argile=repartition(r.get("alea_argile") for r in rows),
        hauteur_m=stats(r.get("hauteur_mean") for r in rows),
    )


async def dpe(code_insee: str) -> dict:
    qs = f'code_insee_ban:"{code_insee}"'
    etiq, typ = await asyncio.gather(*(
        get_json(f"{ADEME}/values_agg", {"field": f, "qs": qs, "agg_size": 20}, source="DPE ADEME")
        for f in ("etiquette_dpe", "type_batiment")
    ))
    total = etiq.get("total", 0)
    if not total:
        raise SourceError(f"Aucun DPE ADEME pour {code_insee}")

    def tab(agg: dict, ordre: bool) -> list[dict]:
        items = [{"modalite": a["value"], "n": a["total"], "part_pct": round(100 * a["total"] / total, 1)} for a in agg.get("aggs", [])]
        return sorted(items, key=lambda i: i["modalite"]) if ordre else items

    passoires = sum(a["total"] for a in etiq.get("aggs", []) if a["value"] in ("F", "G"))
    return reponse(
        f"DPE ADEME · {fr(total)} diagnostics · {code_insee}",
        SRC_DPE,
        nb_dpe=total,
        etiquette_dpe=tab(etiq, True),
        part_passoires_FG_pct=round(100 * passoires / total, 1),
        type_batiment=tab(typ, False),
        avertissement="Un logement peut avoir plusieurs DPE ; ce sont des diagnostics, pas un recensement du parc.",
    )
