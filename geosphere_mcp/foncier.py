"""Foncier : parcelles cadastrales (API Carto IGN) et valeurs foncières DVF géolocalisées (Etalab)."""

from __future__ import annotations

import asyncio
import datetime as dt
import math
from collections import defaultdict
from statistics import median

from .http import SourceError, get_csv, get_json
from .util import departement_de, fr, point_geojson, reponse, stats

APICARTO = "https://apicarto.ign.fr/api"
GEODVF = "https://files.data.gouv.fr/geo-dvf/latest/csv"
SRC_CADASTRE = {"producteur": "DGFiP, PCI Express via API Carto IGN", "url": f"{APICARTO}/cadastre"}
SRC_DVF = {"producteur": "DGFiP, DVF géolocalisées (Etalab)", "url": "https://files.data.gouv.fr/geo-dvf/"}


async def parcelles(lon: float | None, lat: float | None, code_insee: str | None, section: str | None, numero: str | None) -> dict:
    if lon is not None and lat is not None:
        params = {"geom": point_geojson(lon, lat)}
    elif code_insee:
        params = {"code_insee": code_insee, **({"section": section.upper()} if section else {}), **({"numero": numero.zfill(4)} if numero else {})}
    else:
        raise SourceError("Donner lon/lat, ou code_insee (+ section, numéro)")
    params["_limit"] = 1000
    fc = await get_json(f"{APICARTO}/cadastre/parcelle", params, source="API Carto cadastre")
    feats = fc.get("features", [])
    if not feats:
        raise SourceError("Aucune parcelle trouvée")
    props = [f["properties"] for f in feats]
    detail = [{"idu": p["idu"], "section": p["section"], "numero": p["numero"], "contenance_m2": p.get("contenance"),
               "commune": p.get("nom_com"), "code_insee": p.get("code_insee")} for p in props[:20]]
    surf = [p.get("contenance") for p in props]
    return reponse(
        f"Cadastre · {len(props)} parcelle{'s' if len(props) > 1 else ''}",
        SRC_CADASTRE,
        nb_parcelles=len(props),
        contenance_m2=stats(surf) if len(props) > 1 else surf[0],
        parcelles=detail,
        tronque=len(props) > 20,
    )


def _distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6_371_000 * math.hypot(x, y)


async def _annee(dep: str, code: str, an: int) -> list[dict]:
    try:
        return await get_csv(f"{GEODVF}/{an}/communes/{dep}/{code}.csv", source=f"DVF {an}")
    except SourceError:
        return []  # millésime absent du « latest » ou commune sans mutation


async def marche_immobilier(code_insee: str, type_bien: str | None, annees: int, lon: float | None, lat: float | None, rayon_m: float) -> dict:
    if code_insee in ("75056", "13055", "69123"):
        raise SourceError("DVF est publié par arrondissement pour Paris, Marseille et Lyon : utiliser le code d'arrondissement (ex. 75111, 69383)")
    dep = departement_de(code_insee)
    fin = dt.date.today().year
    ans = list(range(fin - annees, fin + 1))
    lignes = [l for rows in await asyncio.gather(*(_annee(dep, code_insee, a) for a in ans)) for l in rows]
    if not lignes:
        raise SourceError(f"Aucune mutation DVF pour {code_insee} sur {ans[0]}–{ans[-1]} (Alsace-Moselle et Mayotte ne sont pas couverts)")

    # Une mutation = plusieurs lignes (une par local / parcelle). On ne garde que les ventes
    # d'un seul logement (appartement ou maison) pour calculer un prix au m² propre.
    mut: dict[str, list[dict]] = defaultdict(list)
    for l in lignes:
        mut[l["id_mutation"]].append(l)
    ventes = []
    for rows in mut.values():
        r0 = rows[0]
        if r0["nature_mutation"] not in ("Vente", "Vente en l'état futur d'achèvement"):
            continue
        logements = [r for r in rows if r["type_local"] in ("Appartement", "Maison")]
        if len(logements) != 1 or not r0["valeur_fonciere"]:
            continue
        lg = logements[0]
        try:
            prix, surf = float(r0["valeur_fonciere"]), float(lg["surface_reelle_bati"] or 0)
        except ValueError:
            continue
        if surf < 9 or prix < 1000:
            continue
        if lon is not None and lat is not None:
            if not lg["longitude"] or _distance_m(lon, lat, float(lg["longitude"]), float(lg["latitude"])) > rayon_m:
                continue
        ventes.append({"annee": int(r0["date_mutation"][:4]), "type": lg["type_local"], "prix": prix, "surface": surf,
                       "pm2": prix / surf, "pieces": lg["nombre_pieces_principales"], "vefa": r0["nature_mutation"] != "Vente"})
    if type_bien:
        ventes = [v for v in ventes if v["type"].lower().startswith(type_bien.lower()[:4])]
    if not ventes:
        raise SourceError("Aucune vente de logement exploitable avec ces filtres")

    par_type = {}
    for t in sorted({v["type"] for v in ventes}):
        vt = [v for v in ventes if v["type"] == t]
        s = stats(round(v["pm2"]) for v in vt)
        par_annee = []
        for a in sorted({v["annee"] for v in vt}):
            va = [v["pm2"] for v in vt if v["annee"] == a]
            par_annee.append({"annee": a, "ventes": len(va), "prix_m2_median": round(median(va))})
        par_type[t] = {"ventes": len(vt), "prix_m2": s, "surface_mediane_m2": round(median(v["surface"] for v in vt)),
                       "prix_median": round(median(v["prix"] for v in vt), -3), "par_annee": par_annee,
                       "part_vefa_pct": round(100 * sum(v["vefa"] for v in vt) / len(vt), 1)}
    perimetre = f"rayon {fr(rayon_m)} m" if lon is not None else f"commune {code_insee}"
    a0, a1 = min(v["annee"] for v in ventes), max(v["annee"] for v in ventes)
    return reponse(
        f"Requête DVF · {fr(len(ventes))} ventes · {a0}–{a1}",
        {**SRC_DVF, "millesimes": f"{a0}–{a1}"},
        perimetre=perimetre,
        par_type=par_type,
        methode="Ventes d'un seul logement (appartement ou maison), dépendances incluses dans le prix ; surfaces < 9 m² écartées.",
    )
