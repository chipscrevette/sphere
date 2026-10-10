"""Tests hors ligne de l'imagerie : scènes Landsat et tuiles d'occupation du sol synthétiques en GeoTIFF local,
catalogue STAC simulé. Vérifie le masque nuages, le composite, l'élargissement de fenêtre et les % de classes."""

from __future__ import annotations

import base64
import json
from urllib.parse import urlparse

import httpx
import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from geosphere_mcp import http as gh
from test_outils import appel

# Petite zone UTM 49N près des Trois-Gorges : 3 km × 3 km
BBOX = [111.0, 30.80, 111.031, 30.827]
CRS = "EPSG:32649"
X0, Y0, RES, N = 499700.0, 3410700.0, 30.0, 120  # 3,6 km de côté, couvre la bbox (≈ 500 000 ; 3 407 400–3 410 430)


def _tif(chemin, data, crs=CRS, x0=X0, y0=Y0, res=RES, nodata=0, dtype="uint16"):
    with rasterio.open(chemin, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1, dtype=dtype,
                       crs=crs, transform=from_origin(x0, y0, res, res), nodata=nodata) as d:
        d.write(data.astype(dtype), 1)
    return str(chemin)


def _dn(r):  # réflectance -> compte numérique Landsat C2 L2
    return np.full((N, N), (r + 0.2) / 0.0000275)


def _scene(tmp, nom, nuage: str | None, date: str):
    """Moitié gauche = eau, moitié droite = forêt. `nuage` masque une moitié via QA_PIXEL (bit 3)."""
    eau = {"red": 0.05, "green": 0.08, "blue": 0.07, "nir08": 0.02, "swir16": 0.01, "swir22": 0.01}
    foret = {"red": 0.03, "green": 0.06, "blue": 0.03, "nir08": 0.40, "swir16": 0.15, "swir22": 0.07}
    assets = {}
    for b in eau:
        a = _dn(eau[b])
        a[:, N // 2:] = _dn(foret[b])[:, N // 2:]
        assets[b] = {"href": _tif(tmp / f"{nom}_{b}.tif", a)}
    qa = np.full((N, N), 21824)  # clair
    if nuage == "gauche":
        qa[:, : N // 2] = 1 << 3
    elif nuage == "droite":
        qa[:, N // 2:] = 1 << 3
    elif nuage == "tout":
        qa[:] = 1 << 3
    assets["qa_pixel"] = {"href": _tif(tmp / f"{nom}_qa.tif", qa, nodata=1)}
    return {"id": nom, "properties": {"datetime": f"{date}T03:00:00Z", "platform": "landsat-5", "eo:cloud_cover": 20}, "assets": assets}


def _classes(tmp, nom, valeurs: dict[int, slice], dtype="uint8"):
    a = np.zeros((N, N))
    for v, sl in valeurs.items():
        a[:, sl] = v
    return _tif(tmp / f"{nom}.tif", a, nodata=0, dtype=dtype)


@pytest.fixture
def stac(tmp_path, monkeypatch):
    s1 = _scene(tmp_path, "L5_ete_a", "gauche", "1995-07-01")
    s2 = _scene(tmp_path, "L5_ete_b", "droite", "1995-08-01")
    tout_nuage = _scene(tmp_path, "L5_2000_nuage", "tout", "2000-07-01")
    clair_hiver = _scene(tmp_path, "L5_2000_hiver", None, "2000-01-15")
    lulc = _classes(tmp_path, "lulc_2020", {1: slice(0, 30), 2: slice(30, 90), 7: slice(90, N)})
    cci = {a: _classes(tmp_path, f"cci_{a}", {210: slice(0, 40 if a == 2005 else 20), 50: slice(40 if a == 2005 else 20, 100), 190: slice(100, N)})
           for a in (1995, 2005)}

    def route(req: httpx.Request) -> httpx.Response:
        u = urlparse(str(req.url))
        if u.netloc == "nominatim.openstreetmap.org":
            return httpx.Response(200, json=[{"lon": "111.0155", "lat": "30.8135", "display_name": "Barrage des Trois-Gorges, Yichang"}])
        if u.path.endswith("/search"):
            corps = json.loads(req.content)
            c, (debut, fin) = corps["collections"][0], corps["datetime"].split("/")
            if c == "landsat-c2-l2":
                feats = [s for s in (s1, s2, tout_nuage, clair_hiver) if debut <= s["properties"]["datetime"] <= fin]
            elif c == "io-lulc-annual-v02":
                feats = [{"id": "lulc", "properties": {"start_datetime": "2020-01-01T00:00:00Z"}, "assets": {"data": {"href": lulc}}}]
            elif c == "esa-cci-lc":
                a = int(debut[:4])
                feats = [{"id": f"cci{a}", "properties": {"start_datetime": f"{a}-01-01T00:00:00Z"},
                          "assets": {"lccs_class": {"href": cci[a]}}}] if a in cci else []
            else:
                feats = []
            return httpx.Response(200, json={"type": "FeatureCollection", "features": feats})
        return httpx.Response(500, text=f"route non simulée {u.netloc}{u.path}")

    gh._cache.clear()
    monkeypatch.setattr(gh, "_client", httpx.AsyncClient(transport=httpx.MockTransport(route)))


_memoires: list[MemoryFile] = []  # garde les fichiers mémoire en vie le temps du test


def _ouvrir(b64: str):
    _memoires.append(MemoryFile(base64.b64decode(b64)))
    return _memoires[-1].open()


async def test_composite_masque_les_nuages(stac):
    r = await appel("composite_satellite", annee=1995, bbox=BBOX, capteur="landsat")
    assert r["nb_scenes"] == 2 and r["pixels_sans_nuage_pct"] == 100.0, r
    assert r["fenetre"] == "mois 5 à 9 de 1995" and "avertissements" not in r
    classes = {o["classe"]: o["part_pct"] for o in r["occupation_indicative"]}
    assert classes["Eau"] > 30 and classes["Forêt / végétation dense"] > 30
    with _ouvrir(r["geotiff_base64"]) as d:
        assert d.count == 6 and d.descriptions[3] == "pir" and d.crs.to_epsg() == 32649
        pir = d.read(4)
        assert pir.max() == 4000  # réflectance 0,40 × 10 000, prise dans la scène sans nuage à droite
    assert r["raster"]["bandes"][0] == "rouge" and r["raster"]["taille_ko"] > 0


async def test_composite_elargit_la_fenetre(stac):
    r = await appel("composite_satellite", annee=2000, bbox=BBOX, capteur="landsat", geotiff=False)
    assert r["fenetre"] == "toute l'année 2000" and r["pixels_sans_nuage_pct"] == 100.0
    assert any("élargie" in a for a in r["avertissements"]) and "geotiff_base64" not in r


async def test_occupation_sol_io_lulc(stac):
    r = await appel("occupation_sol", annee=2020, lieu="Barrage des Trois-Gorges", rayon_km=1.5)
    assert r["produit"] == "io-lulc" and r["zone"]["nom"] == "Barrage des Trois-Gorges"
    parts = {o["classe"]: o["part_pct"] for o in r["occupation"]}
    assert set(parts) == {"Eau", "Forêt", "Bâti"} and abs(sum(parts.values()) - 100) < 0.5
    with _ouvrir(r["geotiff_base64"]) as d:
        assert d.count == 1 and d.colormap(1)[1][:3] == (65, 155, 223)
    assert r["raster"]["legende"][0] == {"code": 1, "classe": "Eau"}


async def test_evolution_meme_produit(stac):
    r = await appel("evolution_occupation_sol", annees=[1995, 2005], bbox=BBOX)
    assert [l["produit"] for l in r["tableau_pct"]] == ["esa-cci", "esa-cci"] and "avertissement" not in r
    assert r["evolution_points"]["variation_points_pct"]["Eau"] > 10  # le réservoir s'étend


async def test_zone_trop_grande(stac):
    r = await appel("occupation_sol", annee=2020, bbox=[100.0, 25.0, 110.0, 35.0])
    assert "300 km" in r["erreur"]


async def test_signature_par_compte(monkeypatch):
    from geosphere_mcp import satellite as sat
    vus = []

    async def faux(url, params=None, *, source):
        vus.append(url)
        return {"token": "sv=1&sig=abc"}

    monkeypatch.setattr(sat, "get_json", faux)
    sat._jetons.clear()
    h = await sat._signer("cop-dem-glo-30", "https://elevationeuwest.blob.core.windows.net/copernicus-dem/COP30_hh/a.tif")
    assert h.endswith("?sv=1&sig=abc") and vus[-1].endswith("/token/elevationeuwest/copernicus-dem")
    assert await sat._signer("x", h) == h  # déjà signé
    assert await sat._signer("x", "https://exemple.org/a.tif") == "https://exemple.org/a.tif"


def test_annee_modis_depuis_identifiant():
    from geosphere_mcp.satellite import _annee_item
    assert _annee_item({"id": "MCD12Q1.A2010001.h27v05.061.2022169161028", "properties": {"datetime": None}}) == "2010"


async def test_geocodage_repli_photon(monkeypatch):
    def route(req: httpx.Request) -> httpx.Response:
        h = urlparse(str(req.url)).netloc
        if h == "nominatim.openstreetmap.org":
            return httpx.Response(429, text="Too many requests")
        if h == "photon.komoot.io":
            return httpx.Response(200, json={"features": [{"geometry": {"coordinates": [111.0, 30.82]}, "properties": {"name": "Three Gorges Dam"}}]})
        return httpx.Response(500)

    gh._cache.clear()
    monkeypatch.setattr(gh, "_client", httpx.AsyncClient(transport=httpx.MockTransport(route)))
    from geosphere_mcp.satellite import emprise
    b, nom = await emprise(None, "Barrage des Trois-Gorges", 5)
    assert nom == "Three Gorges Dam" and b[0] < 111.0 < b[2]


async def test_modis_message_clair(stac):
    r = await appel("occupation_sol", annee=2010, bbox=BBOX, produit="modis")
    assert "esa-cci" in r["erreur"]
