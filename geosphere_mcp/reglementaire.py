"""Réglementaire : urbanisme (Géoportail de l'Urbanisme via API Carto) et risques (Géorisques)."""

from __future__ import annotations

import asyncio
import datetime as dt

from .http import SourceError, get_json
from .util import point_geojson, reponse

APICARTO_GPU = "https://apicarto.ign.fr/api/gpu"
GEORISQUES = "https://georisques.gouv.fr/api/v1"
SRC_GPU = {"producteur": "DGALN / IGN, Géoportail de l'Urbanisme via API Carto", "url": "https://www.geoportail-urbanisme.gouv.fr"}
SRC_GR = {"producteur": "MTE / BRGM, Géorisques (GASPAR, CatNat)", "url": "https://www.georisques.gouv.fr"}


async def _gpu(couche: str, geom: str) -> list[dict]:
    try:
        fc = await get_json(f"{APICARTO_GPU}/{couche}", {"geom": geom}, source=f"GPU {couche}")
    except SourceError:
        return []
    return [f["properties"] for f in fc.get("features", [])]


async def urbanisme(lon: float, lat: float) -> dict:
    geom = point_geojson(lon, lat)
    zones, secteurs, presc, infos, sup, mun = await asyncio.gather(
        *(_gpu(c, geom) for c in ("zone-urba", "secteur-cc", "prescription-surf", "info-surf", "assiette-sup-s", "municipality"))
    )
    rnu = any(m.get("is_rnu") for m in mun)
    zonage = [{
        "zone": z.get("libelle"), "type": z.get("typezone"), "libelle_long": z.get("libelong"),
        "document": z.get("idurba"), "date_approbation": z.get("datappro") or z.get("datvalid"),
        "reglement_pdf": z.get("nomfic") or None,
    } for z in zones] + [{"zone": s.get("libelle"), "type": f"Carte communale ({s.get('typesect')})", "document": s.get("idurba")} for s in secteurs]
    if not zonage and not rnu and not presc and not sup:
        raise SourceError("Aucun document d'urbanisme publié sur le GPU à ce point")
    lib = zonage[0]["zone"] if zonage else ("RNU" if rnu else "sans zonage")
    return reponse(
        f"Urbanisme GPU · zone {lib}",
        SRC_GPU,
        rnu=rnu,
        zonage=zonage,
        prescriptions=[{"libelle": p.get("libelle"), "type": p.get("typepsc"), "detail": p.get("txt")} for p in presc][:15],
        informations=[{"libelle": i.get("libelle"), "type": i.get("typeinf")} for i in infos][:15],
        servitudes_utilite_publique=[{"categorie": s.get("suptype") or s.get("typeass"), "libelle": s.get("nomass") or s.get("nomsuplitt")} for s in sup][:15],
    )


async def _gr(chemin: str, params: dict) -> dict | None:
    try:
        return await get_json(f"{GEORISQUES}/{chemin}", params, source=f"Géorisques {chemin}")
    except SourceError:
        return None


def _date(d: str) -> dt.date:
    j, m, a = d.split("/")
    return dt.date(int(a), int(m), int(j))


async def risques(code_insee: str) -> dict:
    gaspar, catnat, radon, sismo = await asyncio.gather(
        _gr("gaspar/risques", {"code_insee": code_insee}),
        _gr("gaspar/catnat", {"code_insee": code_insee, "page_size": 100}),
        _gr("radon", {"code_insee": code_insee}),
        _gr("zonage_sismique", {"code_insee": code_insee}),
    )
    if not any((gaspar, catnat, radon, sismo)):
        raise SourceError("Géorisques injoignable ou commune inconnue")

    liste = []
    for d in (gaspar or {}).get("data", []):
        for r in d.get("risques_detail", []):
            liste.append({"code": r.get("num_risque"), "risque": r.get("libelle_risque_long")})
    arretes = sorted((catnat or {}).get("data", []), key=lambda a: _date(a["date_debut_evt"]), reverse=True)
    par_type: dict[str, int] = {}
    for a in arretes:
        par_type[a["libelle_risque_jo"]] = par_type.get(a["libelle_risque_jo"], 0) + 1
    rad = ((radon or {}).get("data") or [{}])[0].get("classe_potentiel")
    sis = ((sismo or {}).get("data") or [{}])[0]
    return reponse(
        f"Géorisques · {len(liste)} risques recensés · {len(arretes)} arrêtés CatNat",
        SRC_GR,
        code_insee=code_insee,
        risques_recenses=liste,
        catnat={"nb_arretes": (catnat or {}).get("results", len(arretes)), "par_type": par_type,
                "derniers": [{"debut": a["date_debut_evt"], "type": a["libelle_risque_jo"], "jo": a.get("date_publication_jo")} for a in arretes[:5]]},
        potentiel_radon=f"classe {rad}" if rad else None,
        zone_sismicite=sis.get("zone_sismicite") or sis.get("code_zone"),
    )
