"""Imagerie satellite et occupation du sol, partout dans le monde, via le STAC public de Microsoft Planetary Computer.

- Composites sans nuages : masque par pixel (Landsat QA_PIXEL, Sentinel-2 SCL), médiane des scènes,
  fenêtre de dates élargie automatiquement tant que la zone n'est pas couverte.
- Cartes d'occupation du sol déjà classées (ESA CCI 1992–2020, MODIS 2001+, IO/Esri 2017+, ESA WorldCover).
- Accès générique : chercher une collection, lister les scènes, extraire n'importe quel raster.

Les lectures se font sur les aperçus (overviews) des COG, à la résolution utile seulement :
Render gratuit a 512 Mo de mémoire et la page plafonne vers 2,5 Mpx.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import datetime as dt
import math
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .http import SourceError, get_json, post_json
from .util import fr, reponse

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
SRC_PC = {"producteur": "Microsoft Planetary Computer (catalogue STAC public)", "url": "https://planetarycomputer.microsoft.com/catalog"}
LICENCES = {
    "landsat-c2-l2": "USGS Landsat Collection 2 niveau 2, domaine public",
    "sentinel-2-l2a": "Copernicus Sentinel-2 L2A (ESA), licence Copernicus libre",
    "io-lulc-annual-v02": "Impact Observatory / Esri / Microsoft, CC BY 4.0",
    "esa-worldcover": "ESA WorldCover, CC BY 4.0",
    "esa-cci-lc": "ESA Climate Change Initiative Land Cover, licence ESA CCI",
    "modis-12Q1-061": "NASA MODIS MCD12Q1 v6.1, domaine public",
}

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.TIF",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "VSI_CACHE": "TRUE",
}
MAX_COTE_KM = 300
_jetons: dict[str, tuple[float, str]] = {}


# --- Emprise -------------------------------------------------------------------------------------

async def emprise(bbox: list[float] | None, lieu: str | None, rayon_km: float) -> tuple[list[float], str]:
    """bbox WGS84 [lon_min, lat_min, lon_max, lat_max] et libellé ; depuis une bbox ou un lieu (OpenStreetMap)."""
    if bbox:
        if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise SourceError("bbox attendue : [lon_min, lat_min, lon_max, lat_max] en WGS84")
        b, nom = list(bbox), "emprise donnée"
    elif lieu:
        res = await get_json(NOMINATIM, {"q": lieu, "format": "json", "limit": 1}, source="Géocodage OpenStreetMap")
        if not res:
            raise SourceError(f"Lieu introuvable : « {lieu} ». Donner une bbox [lon_min, lat_min, lon_max, lat_max].")
        lon, lat = float(res[0]["lon"]), float(res[0]["lat"])
        dlat = rayon_km / 111.32
        dlon = rayon_km / (111.32 * math.cos(math.radians(lat)))
        b, nom = [lon - dlon, lat - dlat, lon + dlon, lat + dlat], res[0].get("display_name", lieu).split(",")[0]
    else:
        raise SourceError("Donner une bbox [lon_min, lat_min, lon_max, lat_max] ou un lieu (+ rayon_km)")
    lat0 = (b[1] + b[3]) / 2
    larg = (b[2] - b[0]) * 111.32 * math.cos(math.radians(lat0))
    haut = (b[3] - b[1]) * 111.32
    if max(larg, haut) > MAX_COTE_KM:
        raise SourceError(f"Zone de {larg:.0f} × {haut:.0f} km : {MAX_COTE_KM} km de côté au maximum. Réduire la zone ou la découper.")
    return b, nom


def _grille(bbox: list[float], res_native_m: float, taille_max: int):
    """Grille de sortie en UTM local : résolution native si la zone est petite, sinon dégradée pour tenir dans taille_max px."""
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds

    lon0, lat0 = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    crs = f"EPSG:{(32600 if lat0 >= 0 else 32700) + int((lon0 + 180) // 6) + 1}"
    x0, y0, x1, y1 = transform_bounds("EPSG:4326", crs, *bbox)
    res = max(res_native_m, (x1 - x0) / taille_max, (y1 - y0) / taille_max)
    w, h = max(1, round((x1 - x0) / res)), max(1, round((y1 - y0) / res))
    return crs, from_origin(x0, y1, res, res), w, h, res


# --- Catalogue STAC ------------------------------------------------------------------------------

async def _jeton(collection: str) -> str:
    hit = _jetons.get(collection)
    if hit and hit[0] > time.time():
        return hit[1]
    r = await get_json(f"{SAS}/{collection}", source="Planetary Computer (jeton d'accès)")
    _jetons[collection] = (time.time() + 40 * 60, r["token"])
    return r["token"]


async def _signer(collection: str, href: str) -> str:
    if "blob.core.windows.net" not in href:
        return href
    return f"{href}{'&' if '?' in href else '?'}{await _jeton(collection)}"


async def _chercher(collection: str, bbox: list[float], debut: str, fin: str, nuages_max: float | None = None,
                    limite: int = 100) -> list[dict]:
    corps: dict = {"collections": [collection], "bbox": bbox, "datetime": f"{debut}T00:00:00Z/{fin}T23:59:59Z", "limit": limite}
    if nuages_max is not None:
        corps["query"] = {"eo:cloud_cover": {"lt": nuages_max}}
        corps["sortby"] = [{"field": "eo:cloud_cover", "direction": "asc"}]
    fc = await post_json(f"{STAC}/search", corps, source=f"STAC {collection}")
    return fc.get("features", [])


async def catalogue(recherche: str) -> dict:
    data = await get_json(f"{STAC}/collections", source="Catalogue Planetary Computer")
    mots = [m for m in recherche.lower().split() if m]
    out = []
    for c in data.get("collections", []):
        texte = " ".join([c.get("id", ""), c.get("title", ""), c.get("description", "")[:400], " ".join(c.get("keywords", []))]).lower()
        if all(m in texte for m in mots):
            ext = (c.get("extent", {}).get("temporal", {}).get("interval") or [[None, None]])[0]
            out.append({"id": c["id"], "titre": c.get("title"), "debut": (ext[0] or "")[:10] or None, "fin": (ext[1] or "")[:10] or "en cours",
                        "mots_cles": c.get("keywords", [])[:6]})
    return reponse(f"Catalogue satellite · {len(out)} collection(s) « {recherche} »", SRC_PC, collections=out[:25], tronque=len(out) > 25,
                   conseil="Collection choisie → rechercher_scenes pour voir les dates disponibles, puis extraire_raster.")


async def scenes(collection: str, bbox: list[float], debut: str, fin: str, nuages_max: float | None) -> dict:
    items = await _chercher(collection, bbox, debut, fin, nuages_max, limite=200)
    if not items:
        raise SourceError(f"Aucune scène {collection} sur cette zone entre {debut} et {fin}")
    par_annee: dict[str, int] = {}
    for it in items:
        a = (it["properties"].get("datetime") or it["properties"].get("start_datetime") or "")[:4]
        par_annee[a] = par_annee.get(a, 0) + 1
    meilleures = [{"id": it["id"], "date": (it["properties"].get("datetime") or it["properties"].get("start_datetime") or "")[:10],
                   "nuages_pct": it["properties"].get("eo:cloud_cover"), "plateforme": it["properties"].get("platform"),
                   "assets": sorted(it.get("assets", {}))[:15]} for it in items[:10]]
    return reponse(f"STAC · {collection} · {len(items)} scène(s)", {**SRC_PC, "licence": LICENCES.get(collection, "voir la collection")},
                   nb_scenes=len(items), par_annee=dict(sorted(par_annee.items())), premieres=meilleures, tronque=len(items) >= 200)


# --- Lecture raster ------------------------------------------------------------------------------

def _lire(href: str, crs, transform, w: int, h: int, resampling: str, res_cible: float) -> tuple[np.ndarray, float | None]:
    """Lit une bande COG reprojetée sur la grille cible, depuis l'aperçu le plus grossier qui suffit."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    with rasterio.Env(**GDAL_ENV):
        with rasterio.open(href) as src:
            res_src = abs(src.res[0]) * (111_320 if src.crs and src.crs.is_geographic else 1)
            niveau = None
            for i, f in enumerate(src.overviews(1)):
                if res_src * f <= res_cible:
                    niveau = i
            nodata = src.nodata
        with rasterio.open(href, overview_level=niveau) if niveau is not None else rasterio.open(href) as src:
            with WarpedVRT(src, crs=crs, transform=transform, width=w, height=h, resampling=Resampling[resampling],
                           src_nodata=nodata, nodata=nodata) as vrt:
                return vrt.read(1), nodata


def _en_parallele(taches: list, fonction) -> list:
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(lambda t: fonction(*t), taches))


def _geotiff(data: np.ndarray, crs, transform, nodata, noms: list[str], palette: dict | None = None) -> str:
    from rasterio.io import MemoryFile

    data = data if data.ndim == 3 else data[None]
    profil = {"driver": "GTiff", "width": data.shape[2], "height": data.shape[1], "count": data.shape[0], "dtype": data.dtype.name,
              "crs": crs, "transform": transform, "nodata": nodata, "compress": "deflate", "predictor": 2, "tiled": True,
              "blockxsize": 256, "blockysize": 256}
    if data.shape[2] < 256 or data.shape[1] < 256:
        profil.update(tiled=False, blockxsize=None, blockysize=None)
        profil = {k: v for k, v in profil.items() if v is not None}
    with MemoryFile() as mem:
        with mem.open(**profil) as dst:
            dst.write(data)
            for i, n in enumerate(noms, 1):
                dst.set_band_description(i, n)
            if palette:
                dst.write_colormap(1, palette)
        return base64.b64encode(mem.read()).decode()


def _raster_meta(nom: str, crs, w: int, h: int, res: float, bbox: list[float], bandes: list[str], b64: str, **extra) -> dict:
    return {"nom": nom, "format": "image/tiff", "crs": crs, "largeur": w, "hauteur": h, "resolution_m": round(res, 1),
            "bbox_wgs84": [round(v, 5) for v in bbox], "bandes": bandes, "taille_ko": round(len(b64) * 3 / 4 / 1024), **extra}


# --- Composites sans nuages ----------------------------------------------------------------------

BANDES = ["rouge", "vert", "bleu", "pir", "swir1", "swir2"]
CAPTEURS = {
    "landsat": {"collection": "landsat-c2-l2", "res": 30, "assets": ["red", "green", "blue", "nir08", "swir16", "swir22"], "qa": "qa_pixel"},
    "sentinel-2": {"collection": "sentinel-2-l2a", "res": 10, "assets": ["B04", "B03", "B02", "B08", "B11", "B12"], "qa": "SCL"},
}
SCL_VALIDES = np.array([2, 4, 5, 6, 7, 11])  # ombres de relief, végétation, sol nu, eau, non classé, neige
QA_REJET = 0b11111  # Landsat QA_PIXEL bits 0-4 : remplissage, nuage dilaté, cirrus, nuage, ombre de nuage


def _reflectance(capteur: str, item: dict, brut: np.ndarray) -> np.ndarray:
    x = brut.astype(np.float32)
    if capteur == "landsat":
        r = x * 0.0000275 - 0.2
    else:
        decalage = 1000 if str(item["properties"].get("s2:processing_baseline", "00.00")) >= "04.00" else 0
        r = (x - decalage) / 10000
    r[brut == 0] = np.nan
    return r


def _composite_sync(capteur: str, items: list[dict], crs, transform, w: int, h: int, res: float) -> tuple[np.ndarray, int]:
    cfg = CAPTEURS[capteur]
    pile = np.full((len(items), len(BANDES), h, w), np.nan, dtype=np.float32)
    taches = [(it["assets"][cfg["qa"]]["href"], crs, transform, w, h, "nearest", res) for it in items]
    for k, b in enumerate(cfg["assets"]):
        taches += [(it["assets"][b]["href"], crs, transform, w, h, "average", res) for it in items]
    lus = _en_parallele(taches, _lire)
    n = len(items)
    for i, it in enumerate(items):
        qa = lus[i][0]
        valide = ((qa.astype(np.uint32) & QA_REJET) == 0) & (qa > 0) if capteur == "landsat" else np.isin(qa, SCL_VALIDES)
        for k in range(len(BANDES)):
            r = _reflectance(capteur, it, lus[n * (k + 1) + i][0])
            r[~valide] = np.nan
            pile[i, k] = r
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            comp = np.nanmedian(pile, axis=0)
    nb_obs = np.isfinite(pile[:, 0]).sum(axis=0)
    return comp, int((nb_obs > 0).sum())


def _fenetres(annee: int, mois_debut: int, mois_fin: int) -> list[tuple[str, str, str]]:
    fin_mois = (dt.date(annee + (mois_fin == 12), mois_fin % 12 + 1, 1) - dt.timedelta(days=1)).day
    return [
        (f"{annee}-{mois_debut:02d}-01", f"{annee}-{mois_fin:02d}-{fin_mois}", f"mois {mois_debut} à {mois_fin} de {annee}"),
        (f"{annee}-01-01", f"{annee}-12-31", f"toute l'année {annee}"),
        (f"{annee - 1}-01-01", f"{annee + 1}-12-31", f"{annee - 1} à {annee + 1}"),
    ]


def _capteur_auto(annee: int) -> str:
    return "sentinel-2" if annee >= 2017 else "landsat"


def classes_spectrales(comp: np.ndarray) -> tuple[np.ndarray, list[dict]]:
    """Classification indicative par indices (sans échantillons) : eau, forêt, végétation basse, bâti ou sol nu, autre."""
    r, g, _, nir, sw1, _ = comp
    with np.errstate(all="ignore"):
        ndvi = (nir - r) / (nir + r)
        mndwi = (g - sw1) / (g + sw1)
        ndbi = (sw1 - nir) / (sw1 + nir)
    cl = np.zeros(r.shape, dtype=np.uint8)  # 0 = sans donnée
    ok = np.isfinite(ndvi) & np.isfinite(mndwi)
    cl[ok] = 5
    cl[ok & (ndbi > 0) & (ndvi < 0.3)] = 4
    cl[ok & (ndvi >= 0.3) & (ndvi < 0.6)] = 3
    cl[ok & (ndvi >= 0.6)] = 2
    cl[ok & (mndwi > 0.1) & (ndvi < 0.2)] = 1
    noms = {1: "Eau", 2: "Forêt / végétation dense", 3: "Végétation basse / cultures", 4: "Bâti ou sol nu", 5: "Autre / végétation clairsemée"}
    return cl, [{"code": c, "classe": noms[c]} for c in noms]


def _parts(cl: np.ndarray, legende: list[dict], res: float, exclure: tuple[int, ...] = (0,)) -> list[dict]:
    vals, n = np.unique(cl, return_counts=True)
    compte = dict(zip(vals.tolist(), n.tolist()))
    total = sum(v for k, v in compte.items() if k not in exclure) or 1
    km2 = res * res / 1e6
    return [{"classe": l["classe"], "part_pct": round(100 * compte.get(l["code"], 0) / total, 1),
             "surface_km2": round(compte.get(l["code"], 0) * km2, 2)} for l in legende if compte.get(l["code"], 0)]


async def composite(bbox: list[float] | None, lieu: str | None, rayon_km: float, annee: int, capteur: str, mois_debut: int, mois_fin: int,
                    max_scenes: int, taille_max: int, classer: bool, geotiff: bool) -> dict:
    zone, nom = await emprise(bbox, lieu, rayon_km)
    capteur = _capteur_auto(annee) if capteur == "auto" else capteur
    if capteur == "sentinel-2" and annee < 2016:
        raise SourceError("Sentinel-2 commence en 2015-2016 : utiliser capteur='landsat' avant 2017")
    if annee < 1984:
        raise SourceError("Landsat TM commence en 1984 : pas de composite plus ancien ici")
    cfg = CAPTEURS[capteur]
    crs, transform, w, h, res = _grille(zone, cfg["res"], taille_max)
    avert = []
    items, fenetre, comp, couverts = [], None, None, 0
    for debut, fin, libelle in _fenetres(annee, mois_debut, mois_fin):
        trouves = await _chercher(cfg["collection"], zone, debut, fin, nuages_max=70)
        if capteur == "landsat":  # Landsat 7 après 2003 : bandes manquantes (SLC-off), en dernier recours seulement
            propres = [it for it in trouves if not (it["properties"].get("platform") == "landsat-7" and it["properties"]["datetime"] >= "2003-06")]
            trouves = propres if len(propres) >= 3 else trouves
        if not trouves:
            continue
        items = [copy.deepcopy(it) for it in trouves[:max_scenes]]  # le cache HTTP partage les objets : on signe une copie
        for it in items:
            for a in [cfg["qa"], *cfg["assets"]]:
                it["assets"][a]["href"] = await _signer(cfg["collection"], it["assets"][a]["href"])
        comp, couverts = await asyncio.to_thread(_composite_sync, capteur, items, crs, transform, w, h, res)
        fenetre = libelle
        if couverts / (w * h) >= 0.95:
            break
        avert.append(f"{libelle} : {round(100 * couverts / (w * h))} % de la zone sans nuage, fenêtre élargie")
    if comp is None:
        raise SourceError(f"Aucune scène {capteur} exploitable autour de {annee} sur cette zone")
    valides = round(100 * couverts / (w * h), 1)
    if valides < 95:
        avert.append(f"Seulement {valides} % de la zone couverte sans nuage : trous en nodata dans le composite")
    dates = sorted(it["properties"]["datetime"][:10] for it in items)
    out = {"zone": {"nom": nom, "bbox_wgs84": [round(v, 5) for v in zone]}, "capteur": capteur, "fenetre": fenetre,
           "nb_scenes": len(items), "dates": [dates[0], dates[-1]], "pixels_sans_nuage_pct": valides,
           "plateformes": sorted({it["properties"].get("platform") for it in items})}
    with np.errstate(all="ignore"):
        ndvi = (comp[3] - comp[0]) / (comp[3] + comp[0])
    out["ndvi_median"] = round(float(np.nanmedian(ndvi)), 3) if np.isfinite(ndvi).any() else None
    if classer:
        cl, legende = classes_spectrales(comp)
        out["occupation_indicative"] = _parts(cl, legende, res)
        out["methode_classes"] = ("Seuils d'indices (NDVI, MNDWI, NDBI) sans échantillons : indicatif. Bâti et sol nu se confondent ; "
                                  "pour des % fiables, occupation_sol (cartes déjà classées) ou une classification supervisée dans Géosphère.")
    if avert:
        out["avertissements"] = avert
    if geotiff:
        data = np.where(np.isfinite(comp), np.clip(np.round(comp * 10000), -1000, 20000), -32768).astype(np.int16)
        b64 = _geotiff(data, crs, transform, -32768, BANDES)
        out["raster"] = _raster_meta(f"{capteur} {annee} {nom}", crs, w, h, res, zone, BANDES, b64,
                                     unite="réflectance × 10 000", nodata=-32768)
        out["geotiff_base64"] = b64
    return reponse(f"Composite {capteur} {annee} · {len(items)} scènes · {valides} % sans nuage · {w}×{h} px à {res:.0f} m",
                   {**SRC_PC, "licence": LICENCES[cfg["collection"]], "collection": cfg["collection"]}, **out)


# --- Occupation du sol déjà classée --------------------------------------------------------------

HARMO = ["Eau", "Forêt", "Arbustes / prairies", "Cultures", "Bâti", "Sol nu", "Zones humides", "Neige / glace"]
PRODUITS = {
    "io-lulc": {"collection": "io-lulc-annual-v02", "annees": (2017, 2023), "res": 10, "asset": "data",
                "classes": {1: ("Eau", "Eau"), 2: ("Arbres", "Forêt"), 4: ("Végétation inondée", "Zones humides"), 5: ("Cultures", "Cultures"),
                            7: ("Zones bâties", "Bâti"), 8: ("Sol nu", "Sol nu"), 9: ("Neige / glace", "Neige / glace"), 11: ("Parcours", "Arbustes / prairies")},
                "ignorer": (0, 10)},
    "esa-worldcover": {"collection": "esa-worldcover", "annees": (2020, 2021), "res": 10, "asset": "map",
                       "classes": {10: ("Couvert arboré", "Forêt"), 20: ("Arbustes", "Arbustes / prairies"), 30: ("Prairies", "Arbustes / prairies"),
                                   40: ("Cultures", "Cultures"), 50: ("Bâti", "Bâti"), 60: ("Sol nu / végétation clairsemée", "Sol nu"),
                                   70: ("Neige / glace", "Neige / glace"), 80: ("Eau permanente", "Eau"), 90: ("Zones humides herbacées", "Zones humides"),
                                   95: ("Mangroves", "Zones humides"), 100: ("Mousses et lichens", "Arbustes / prairies")},
                       "ignorer": (0,)},
    "esa-cci": {"collection": "esa-cci-lc", "annees": (1992, 2020), "res": 300, "asset": "lccs_class",
                "classes": {**{c: ("Cultures", "Cultures") for c in (10, 11, 12, 20)}, 30: ("Mosaïque cultures > 50 %", "Cultures"),
                            40: ("Mosaïque végétation naturelle > 50 %", "Arbustes / prairies"),
                            **{c: ("Forêt", "Forêt") for c in (50, 60, 61, 62, 70, 71, 72, 80, 81, 82, 90, 100)},
                            110: ("Mosaïque herbacée", "Arbustes / prairies"), 120: ("Arbustes", "Arbustes / prairies"), 121: ("Arbustes", "Arbustes / prairies"),
                            122: ("Arbustes", "Arbustes / prairies"), 130: ("Prairies", "Arbustes / prairies"), 140: ("Lichens et mousses", "Arbustes / prairies"),
                            150: ("Végétation clairsemée", "Sol nu"), 151: ("Végétation clairsemée", "Sol nu"), 152: ("Végétation clairsemée", "Sol nu"),
                            153: ("Végétation clairsemée", "Sol nu"), 160: ("Forêt inondée", "Zones humides"), 170: ("Forêt inondée (eau salée)", "Zones humides"),
                            180: ("Végétation inondée", "Zones humides"), 190: ("Zones urbaines", "Bâti"), 200: ("Sol nu", "Sol nu"), 201: ("Sol nu", "Sol nu"),
                            202: ("Sol nu", "Sol nu"), 210: ("Eau", "Eau"), 220: ("Neige / glace", "Neige / glace")},
                "ignorer": (0,)},
    "modis": {"collection": "modis-12Q1-061", "annees": (2001, 2023), "res": 500, "asset": "LC_Type1",
              "classes": {**{c: ("Forêt", "Forêt") for c in (1, 2, 3, 4, 5)}, 6: ("Fourrés denses", "Arbustes / prairies"), 7: ("Fourrés ouverts", "Arbustes / prairies"),
                          8: ("Savanes arborées", "Arbustes / prairies"), 9: ("Savanes", "Arbustes / prairies"), 10: ("Prairies", "Arbustes / prairies"),
                          11: ("Zones humides permanentes", "Zones humides"), 12: ("Cultures", "Cultures"), 13: ("Zones urbaines", "Bâti"),
                          14: ("Mosaïque cultures / végétation", "Cultures"), 15: ("Neige / glace", "Neige / glace"), 16: ("Sol nu", "Sol nu"), 17: ("Eau", "Eau")},
              "ignorer": (0, 255)},
}
PALETTE_HARMO = {0: (0, 0, 0, 0), 1: (65, 155, 223, 255), 2: (57, 125, 73, 255), 3: (180, 200, 120, 255), 4: (228, 200, 90, 255),
                 5: (196, 40, 27, 255), 6: (165, 155, 143, 255), 7: (122, 135, 198, 255), 8: (230, 240, 250, 255)}


def produit_auto(annee: int) -> str:
    if 2017 <= annee <= 2023:
        return "io-lulc"
    if 1992 <= annee <= 2020:
        return "esa-cci"
    if 2001 <= annee <= 2023:
        return "modis"
    raise SourceError(f"Pas de carte d'occupation du sol déjà classée pour {annee} (1992–2023). "
                      "Pour 1984–1991 : composite_satellite(classer=True), ou classification supervisée dans Géosphère.")


def _items_annee(items: list[dict], annee: int) -> list[dict]:
    def an(it):
        p = it["properties"]
        return (p.get("start_datetime") or p.get("datetime") or "")[:4]
    return [it for it in items if an(it) == str(annee)]


def _mosaique_sync(hrefs: list[str], crs, transform, w: int, h: int, res: float, ignorer: tuple[int, ...]) -> np.ndarray:
    lus = _en_parallele([(hr, crs, transform, w, h, "mode", res) for hr in hrefs], _lire)
    out = np.zeros((h, w), dtype=np.uint16)
    for arr, nodata in lus:
        a = arr.astype(np.uint16)
        vide = (out == 0) | np.isin(out, ignorer)
        prend = vide & ~np.isin(a, ignorer) & ((a != nodata) if nodata is not None else True)
        out[prend] = a[prend]
    return out


async def occupation_sol(bbox: list[float] | None, lieu: str | None, rayon_km: float, annee: int, produit: str, taille_max: int, geotiff: bool) -> dict:
    zone, nom = await emprise(bbox, lieu, rayon_km)
    produit = produit_auto(annee) if produit == "auto" else produit
    p = PRODUITS[produit]
    a0, a1 = p["annees"]
    if not a0 <= annee <= a1:
        raise SourceError(f"{produit} couvre {a0}–{a1} seulement")
    items = await _chercher(p["collection"], zone, f"{annee}-01-01", f"{annee}-12-31", limite=50)
    items = _items_annee(items, annee) or items
    if not items:
        raise SourceError(f"Aucune tuile {produit} {annee} sur cette zone")
    hrefs = []
    for it in items:
        asset = it["assets"].get(p["asset"]) or next(v for v in it["assets"].values() if "image/tiff" in v.get("type", ""))
        hrefs.append(await _signer(p["collection"], asset["href"]))
    crs, transform, w, h, res = _grille(zone, p["res"], taille_max)
    brut = await asyncio.to_thread(_mosaique_sync, hrefs, crs, transform, w, h, res, p["ignorer"])
    detail = _parts(brut, [{"code": c, "classe": f"{lib} ({c})"} for c, (lib, _) in p["classes"].items()], res, exclure=p["ignorer"])
    harmo = np.zeros(brut.shape, dtype=np.uint8)
    for c, (_, g) in p["classes"].items():
        harmo[brut == c] = HARMO.index(g) + 1
    legende = [{"code": i + 1, "classe": g} for i, g in enumerate(HARMO)]
    connus = np.isin(brut, list(p["classes"])).sum()
    out = {"zone": {"nom": nom, "bbox_wgs84": [round(v, 5) for v in zone]}, "produit": produit, "annee": annee,
           "resolution_m": round(res), "occupation": _parts(harmo, legende, res), "detail": detail[:15],
           "pixels_classes_pct": round(100 * connus / (w * h), 1)}
    if res > p["res"] * 1.5:
        out["avertissement"] = f"Zone grande : lue à {res:.0f} m au lieu de {p['res']} m natifs (petites surfaces lissées)."
    if geotiff:
        b64 = _geotiff(harmo, crs, transform, 0, ["occupation"], palette=PALETTE_HARMO)
        out["raster"] = _raster_meta(f"Occupation {produit} {annee} {nom}", crs, w, h, res, zone, ["occupation"], b64, nodata=0,
                                     legende=legende, couleurs={str(k): "#%02x%02x%02x" % v[:3] for k, v in PALETTE_HARMO.items() if k})
        out["geotiff_base64"] = b64
    princ = max(out["occupation"], key=lambda o: o["part_pct"]) if out["occupation"] else None
    return reponse(f"Occupation du sol {produit} {annee} · {nom}" + (f" · {princ['classe']} {fr(princ['part_pct'], 1)} %" if princ else ""),
                   {**SRC_PC, "licence": LICENCES[p["collection"]], "collection": p["collection"]}, **out)


# --- Extraction générique ------------------------------------------------------------------------

async def extraire(collection: str, asset: str, bbox: list[float] | None, lieu: str | None, rayon_km: float, debut: str, fin: str,
                   taille_max: int, res_native_m: float) -> dict:
    zone, nom = await emprise(bbox, lieu, rayon_km)
    items = await _chercher(collection, zone, debut, fin, limite=50)
    if not items:
        raise SourceError(f"Aucun élément {collection} entre {debut} et {fin} sur cette zone")
    if asset not in items[0].get("assets", {}):
        raise SourceError(f"Asset « {asset} » absent. Assets disponibles : {', '.join(sorted(items[0]['assets']))}")
    hrefs = [await _signer(collection, it["assets"][asset]["href"]) for it in items if asset in it["assets"]]
    crs, transform, w, h, res = _grille(zone, res_native_m, taille_max)

    def lire_tout():
        lus = _en_parallele([(hr, crs, transform, w, h, "bilinear", res) for hr in hrefs], _lire)
        out, nodata = lus[0]
        out = out.astype(np.float32)
        manque = (out == nodata) if nodata is not None else np.zeros(out.shape, bool)
        for arr, nd in lus[1:]:
            prend = manque & ((arr != nd) if nd is not None else True)
            out[prend] = arr[prend]
            manque &= ~prend
        out[manque] = np.nan
        return out

    data = await asyncio.to_thread(lire_tout)
    ok = data[np.isfinite(data)]
    stats = {"min": float(ok.min()), "mediane": float(np.median(ok)), "max": float(ok.max())} if ok.size else None
    b64 = _geotiff(np.where(np.isfinite(data), data, -9999).astype(np.float32), crs, transform, -9999, [asset])
    return reponse(f"Raster {collection}/{asset} · {nom} · {w}×{h} px à {res:.0f} m",
                   {**SRC_PC, "licence": LICENCES.get(collection, "voir la collection"), "collection": collection},
                   zone={"nom": nom, "bbox_wgs84": [round(v, 5) for v in zone]}, nb_elements=len(hrefs), statistiques=stats,
                   raster=_raster_meta(f"{collection} {asset} {nom}", crs, w, h, res, zone, [asset], b64, nodata=-9999),
                   geotiff_base64=b64)


async def evolution(bbox: list[float] | None, lieu: str | None, rayon_km: float, annees: list[int], produit: str, taille_max: int) -> dict:
    """Série d'occupation du sol sur plusieurs années, même produit autant que possible pour comparer ce qui est comparable."""
    annees = sorted(set(annees))
    if produit == "auto":
        produit = "esa-cci" if all(1992 <= a <= 2020 for a in annees) else ("io-lulc" if all(2017 <= a <= 2023 for a in annees) else "auto")
    zone, nom = await emprise(bbox, lieu, rayon_km)
    lignes, produits = [], set()
    for a in annees:
        try:
            r = await occupation_sol(zone, None, rayon_km, a, produit, taille_max, geotiff=False)
        except SourceError as e:
            lignes.append({"annee": a, "erreur": str(e)})
            continue
        produits.add(r["produit"])
        lignes.append({"annee": a, "produit": r["produit"], **{o["classe"]: o["part_pct"] for o in r["occupation"]}})
    ok = [l for l in lignes if "erreur" not in l]
    if not ok:
        raise SourceError("Aucune année exploitable : " + "; ".join(l["erreur"] for l in lignes))
    evol = {c: round(ok[-1].get(c, 0) - ok[0].get(c, 0), 1) for c in HARMO if any(c in l for l in ok)}
    out = {"zone": {"nom": nom, "bbox_wgs84": [round(v, 5) for v in zone]}, "tableau_pct": lignes,
           "evolution_points": {"de": ok[0]["annee"], "a": ok[-1]["annee"], "variation_points_pct": evol}}
    if len(produits) > 1:
        out["avertissement"] = (f"Produits mélangés ({', '.join(sorted(produits))}) : résolutions et nomenclatures différentes, "
                                "les écarts entre produits ne sont pas tous des changements réels.")
    return reponse(f"Évolution occupation du sol · {nom} · {ok[0]['annee']}–{ok[-1]['annee']} · {len(ok)} années",
                   {**SRC_PC, "licence": "; ".join(LICENCES[PRODUITS[p]['collection']] for p in sorted(produits))}, **out)
