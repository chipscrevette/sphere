"""Tests hors ligne : chaque outil est appelé via le protocole MCP contre des réponses enregistrées.

Les fixtures reprennent la forme des vraies réponses (relevées le 2026-10-09 sur Villeurbanne, 69266),
réduites à quelques objets. `pytest -m live` lance en plus les appels réels (réseau requis).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from geosphere_mcp import http as gh
from geosphere_mcp.server import mcp

COMMUNE = {"code": "69266", "nom": "Villeurbanne", "population": 163684, "surface": 1490.12,
           "centre": {"type": "Point", "coordinates": [4.8898, 45.7719]}, "codesPostaux": ["69100"],
           "codeEpci": "200046977", "epci": {"code": "200046977", "nom": "Métropole de Lyon"},
           "departement": {"code": "69", "nom": "Rhône"}, "region": {"code": "84", "nom": "Auvergne-Rhône-Alpes"}}

BAN = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [4.8801, 45.7702]},
       "properties": {"type": "housenumber", "label": "30 Rue Jean Bourgey 69100 Villeurbanne", "postcode": "69100", "citycode": "69266",
                      "city": "Villeurbanne", "context": "69, Rhône, Auvergne-Rhône-Alpes", "id": "69266_2110_00030",
                      "x": 846115.02, "y": 6520672.79, "distance": 22, "score": 0.9978}}]}

PARCELLE = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": None, "properties": {
    "numero": "0082", "feuille": 1, "section": "BD", "code_dep": "69", "nom_com": "Villeurbanne", "code_com": "266",
    "idu": "69266000BD0082", "contenance": 3728, "code_insee": "69266"}}]}

ZONE = {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": None, "properties": {
    "libelle": "UPr4", "libelong": "Cette zone regroupe les secteurs qui font l objet d un renouvellement urbain a vocation mixte.",
    "typezone": "U", "nomfic": "200046977_reglement_20260326.pdf", "datvalid": "20260326", "idurba": "200046977_PLUI_20260326"}}]}

CATNAT = {"results": 2, "page": 1, "total_pages": 1, "response_code": 200, "data": [
    {"code_national_catnat": "INTE2127287A", "date_debut_evt": "23/06/2021", "date_fin_evt": "23/06/2021",
     "date_publication_jo": "28/09/2021", "libelle_risque_jo": "Inondations et/ou Coulées de Boue", "code_insee": "69266"},
    {"code_national_catnat": "INTE9900627A", "date_debut_evt": "25/12/1999", "date_fin_evt": "29/12/1999",
     "date_publication_jo": "30/12/1999", "libelle_risque_jo": "Inondations, coulées de boue et mouvements de terrain", "code_insee": "69266"}]}

GASPAR = {"results": 1, "data": [{"code_insee": "69266", "libelle_commune": "VILLEURBANNE", "risques_detail": [
    {"num_risque": "11", "libelle_risque_long": "Inondation"}, {"num_risque": "13", "libelle_risque_long": "Séisme"},
    {"num_risque": "18", "libelle_risque_long": "Radon"}]}]}

BDNB = [
    {"batiment_groupe_id": "bdnb-bg-3A3B-2PY9-1T61", "libelle_adr_principale_ban": "14 Rue Georges Courteline 69100 Villeurbanne",
     "l_parcelle_id": ["69266000BB0392"], "annee_construction": 1961, "usage_principal_bdnb_open": "Résidentiel collectif",
     "nb_log": 2, "nb_niveau": 3, "hauteur_mean": 8, "surface_emprise_sol": 243, "mat_mur_txt": "BETON", "classe_bilan_dpe": None,
     "alea_argile": "Moyen", "l_denomination_proprietaire": ["ELB PROMOTION"]},
    {"batiment_groupe_id": "bdnb-bg-95TF-7VNX-LXME", "l_parcelle_id": ["69266000AL0003"], "hauteur_mean": 4,
     "surface_emprise_sol": 6, "alea_argile": "Moyen"},
]

DPE_ETIQ = {"total": 51951, "aggs": [{"total": 26383, "value": "C"}, {"total": 14915, "value": "D"}, {"total": 5782, "value": "E"},
            {"total": 1855, "value": "B"}, {"total": 1844, "value": "F"}, {"total": 983, "value": "G"}, {"total": 189, "value": "A"}]}
DPE_TYPE = {"total": 51951, "aggs": [{"total": 47000, "value": "appartement"}, {"total": 4951, "value": "maison"}]}

WFS_BATI = {"type": "FeatureCollection", "numberMatched": 2, "numberReturned": 2, "features": [
    {"type": "Feature", "geometry": None, "properties": {"cleabs": "BATIMENT0000000241309395", "nature": "Industriel, agricole ou commercial",
     "usage_1": "Indifférencié", "hauteur": 4.5, "nombre_d_etages": None, "nombre_de_logements": None, "materiaux_des_murs": None}},
    {"type": "Feature", "geometry": None, "properties": {"cleabs": "BATIMENT0000000241309396", "nature": "Indifférenciée",
     "usage_1": "Résidentiel", "hauteur": 15.0, "nombre_d_etages": 5, "nombre_de_logements": 24, "materiaux_des_murs": "Béton"}}]}

DVF_TETE = ("id_mutation,date_mutation,numero_disposition,nature_mutation,valeur_fonciere,adresse_numero,adresse_suffixe,"
            "adresse_nom_voie,adresse_code_voie,code_postal,code_commune,nom_commune,code_departement,ancien_code_commune,"
            "ancien_nom_commune,id_parcelle,ancien_id_parcelle,numero_volume,lot1_numero,lot1_surface_carrez,lot2_numero,"
            "lot2_surface_carrez,lot3_numero,lot3_surface_carrez,lot4_numero,lot4_surface_carrez,lot5_numero,lot5_surface_carrez,"
            "nombre_lots,code_type_local,type_local,surface_reelle_bati,nombre_pieces_principales,code_nature_culture,"
            "nature_culture,code_nature_culture_speciale,nature_culture_speciale,surface_terrain,longitude,latitude")


def _dvf(id_, date, nature, prix, type_, surf, pieces, lon=4.88, lat=45.77):
    code = {"Appartement": "2", "Maison": "1", "Dépendance": "3"}[type_]
    return (f"{id_},{date},1,{nature},{prix},14,,RUE TEST,1840,69100,69266,Villeurbanne,69,,,69266000BB0392,,,,,,,,,,,,,1,"
            f"{code},{type_},{surf},{pieces},,,,,,{lon},{lat}")


DVF_2024 = "\n".join([DVF_TETE,
    _dvf("2024-1", "2024-03-01", "Vente", 250000, "Appartement", 50, 2),
    _dvf("2024-1", "2024-03-01", "Vente", 250000, "Dépendance", "", ""),  # cave rattachée : même mutation
    _dvf("2024-2", "2024-05-10", "Vente", 400000, "Appartement", 80, 4),
    _dvf("2024-3", "2024-06-01", "Vente", 520000, "Maison", 110, 5, lon=4.90, lat=45.78),
    _dvf("2024-4", "2024-07-01", "Echange", 100000, "Appartement", 30, 1),
])


def _route(req: httpx.Request) -> httpx.Response:
    u = urlparse(str(req.url))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    h, p = u.netloc, u.path
    if h == "geo.api.gouv.fr" and p == "/communes/69266":
        return httpx.Response(200, json=COMMUNE)
    if h == "geo.api.gouv.fr" and p == "/communes":
        return httpx.Response(200, json=[{**COMMUNE, "_score": 1}])
    if h == "data.geopf.fr" and p.startswith("/geocodage/"):
        return httpx.Response(200, json=BAN)
    if h == "apicarto.ign.fr" and p == "/api/cadastre/parcelle":
        return httpx.Response(200, json=PARCELLE)
    if h == "apicarto.ign.fr" and p == "/api/gpu/zone-urba":
        return httpx.Response(200, json=ZONE)
    if h == "apicarto.ign.fr" and p.startswith("/api/gpu/"):
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})
    if h == "georisques.gouv.fr":
        return {"/api/v1/gaspar/catnat": httpx.Response(200, json=CATNAT),
                "/api/v1/gaspar/risques": httpx.Response(200, json=GASPAR)}.get(p, httpx.Response(404))
    if h == "api.bdnb.io" and p.endswith("/rel_batiment_groupe_parcelle"):
        return httpx.Response(200, json=[{"batiment_groupe_id": "bdnb-bg-3A3B-2PY9-1T61", "parcelle_id": "69266000BB0392"}])
    if h == "api.bdnb.io":
        return httpx.Response(200, json=BDNB[:1] if "batiment_groupe_id" in q else BDNB)
    if h == "data.ademe.fr":
        return httpx.Response(200, json=DPE_ETIQ if q["field"] == "etiquette_dpe" else DPE_TYPE)
    if h == "data.geopf.fr" and p == "/wfs/ows":
        return httpx.Response(200, json=WFS_BATI)
    if h == "files.data.gouv.fr":
        return httpx.Response(200, text=DVF_2024) if "/2024/" in p else httpx.Response(404)
    return httpx.Response(500, text=f"route non simulée {h}{p}")


@pytest.fixture(autouse=True)
def faux_reseau(request, monkeypatch):
    gh._cache.clear()
    if "live" not in request.keywords:
        monkeypatch.setattr(gh, "_client", httpx.AsyncClient(transport=httpx.MockTransport(_route)))
    else:
        monkeypatch.setattr(gh, "_client", None)


async def appel(nom: str, **args) -> dict:
    res = await mcp.call_tool(nom, args)
    blocs = res[0] if isinstance(res, tuple) else res
    if hasattr(blocs, "structured_content") and blocs.structured_content:
        return blocs.structured_content
    contenu = getattr(blocs, "content", blocs)
    return json.loads(contenu[0].text)


async def test_resoudre_territoire():
    r = await appel("resoudre_territoire", requete="69266")
    c = r["resultats"][0]
    assert r["etape"] == "Territoire · Villeurbanne (69266)"
    assert c["densite_hab_km2"] == 10985 and c["epci"]["nom"] == "Métropole de Lyon"


async def test_geocoder():
    r = await appel("geocoder", adresse="30 rue Jean Bourgey Villeurbanne")
    assert r["resultats"][0]["code_insee"] == "69266"
    assert r["etape"].startswith("Géocodage BAN · 30 Rue Jean Bourgey")


async def test_parcelles():
    r = await appel("parcelles", lon=4.88, lat=45.77)
    assert r["parcelles"][0]["idu"] == "69266000BD0082" and r["contenance_m2"] == 3728


async def test_marche_immobilier_ignore_echanges_et_dependances():
    r = await appel("marche_immobilier", code_insee="69266")
    assert r["etape"].startswith("Requête DVF · 3 ventes")
    app = r["par_type"]["Appartement"]
    assert app["ventes"] == 2 and app["prix_m2"]["mediane"] == 5000  # 5 000 et 5 000 €/m²


async def test_marche_immobilier_rayon():
    r = await appel("marche_immobilier", code_insee="69266", lon=4.88, lat=45.77, rayon_m=200)
    assert set(r["par_type"]) == {"Appartement"}  # la maison est à ~1,8 km


async def test_marche_immobilier_refuse_commune_a_arrondissements():
    r = await appel("marche_immobilier", code_insee="69123")
    assert "arrondissement" in r["erreur"]


async def test_batiments_commune_et_parcelle():
    r = await appel("batiments", code_insee="69266")
    assert r["nb_batiments"] == 2 and r["logements_total"] == 2
    p = await appel("batiments", parcelle="69266000BB0392")
    assert p["batiments"][0]["annee_construction"] == 1961


async def test_batiments_autour():
    r = await appel("batiments_autour", lon=4.88, lat=45.77, rayon_m=100)
    assert r["nb_batiments"] == 2 and r["logements_total"] == 24


async def test_dpe():
    r = await appel("dpe_logements", code_insee="69266")
    assert r["part_passoires_FG_pct"] == 5.4 and r["etiquette_dpe"][0]["modalite"] == "A"


async def test_urbanisme():
    r = await appel("urbanisme", lon=4.88, lat=45.77)
    assert r["etape"] == "Urbanisme GPU · zone UPr4" and r["zonage"][0]["type"] == "U"


async def test_risques():
    r = await appel("risques", code_insee="69266")
    assert r["catnat"]["nb_arretes"] == 2 and r["catnat"]["derniers"][0]["debut"] == "23/06/2021"
    assert len(r["risques_recenses"]) == 3


async def test_source_toujours_citee():
    r = await appel("dpe_logements", code_insee="69266")
    assert r["source"]["licence"].startswith("Licence Ouverte") and r["source"]["producteur"]


# --- Appels réels : pytest -m live -------------------------------------------------------------

@pytest.mark.live
@pytest.mark.parametrize("nom,args", [
    ("resoudre_territoire", {"requete": "Villeurbanne"}),
    ("resoudre_territoire", {"requete": "200046977"}),
    ("geocoder", {"adresse": "20 avenue de Ségur Paris"}),
    ("parcelles", {"lon": 4.88, "lat": 45.77}),
    ("marche_immobilier", {"code_insee": "69266"}),
    ("batiments", {"code_insee": "69266", "max_batiments": 2000}),
    ("batiments", {"parcelle": "69266000BB0392"}),
    ("batiments_autour", {"lon": 4.88, "lat": 45.77}),
    ("dpe_logements", {"code_insee": "69266"}),
    ("urbanisme", {"lon": 4.88, "lat": 45.77}),
    ("risques", {"code_insee": "69266"}),
    ("lister_couches_ign", {"recherche": "rpg"}),
    ("interroger_couche_ign", {"couche": "BDTOPO_V3:troncon_de_route", "lon": 4.88, "lat": 45.77}),
])
async def test_live(nom, args):
    r = await appel(nom, **args)
    print(f"\n{nom}: {r['etape']}")
    assert "erreur" not in r, r.get("erreur")
