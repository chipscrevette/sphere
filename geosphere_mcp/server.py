"""Serveur MCP Géosphère : données SIG françaises en direct, résumées pour le modèle.

Lancement :
    python -m geosphere_mcp            # stdio (Claude Desktop, Claude Code)
    python -m geosphere_mcp --http     # HTTP streamable sur :8000/mcp (backend Géosphère, connecteur claude.ai)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Literal

from pydantic import Field

from mcp.server.mcpserver import MCPServer

from . import batiments as bati, fond as fnd, foncier, ign, reglementaire as regl, satellite as sat, territoire as terr
from .http import SourceError

INSTRUCTIONS = """Données SIG en direct : France (IGN, INSEE/COG, BAN, cadastre, DVF, BDNB, DPE ADEME, GPU, Géorisques) et monde (satellite, occupation du sol).
En France, commencer par resoudre_territoire ou geocoder pour obtenir code INSEE et coordonnées, puis appeler l'outil métier.
Chaque réponse porte `etape` (ligne à afficher dans le fil d'étapes) et `source` (à citer avec le millésime).
Les résultats sont déjà agrégés : ne pas redemander le détail brut sauf besoin explicite.
Pour AFFICHER des entités sur la carte (ventes une par une, bâtiments, parcelles agricoles…), utiliser ventes_carte ou entites_ign :
leur clé `geojson` est une couche à charger telle quelle ; ne jamais la recopier dans la réponse.
Hors de France ou en imagerie : composite_satellite (images sans nuages), occupation_sol et evolution_occupation_sol (% eau, forêt, bâti…),
extraire_raster pour tout autre raster mondial. Leur clé `geotiff_base64` est un raster à charger tel quel ; ne jamais la recopier.
evolution_occupation_sol renvoie une couche par année (`couches`) et une couche `changements`, superposables pour comparer."""

mcp = MCPServer("geosphere", title="Géosphère · données SIG", instructions=INSTRUCTIONS, version="0.6.0")

Lon = Annotated[float, Field(description="Longitude WGS84", ge=-180, le=180)]
Lat = Annotated[float, Field(description="Latitude WGS84", ge=-90, le=90)]
CodeInsee = Annotated[str, Field(description="Code commune INSEE (5 caractères, ex. 69266 ; arrondissement pour Paris/Lyon/Marseille)", pattern=r"^(\d{5}|2[ABab]\d{3})$")]
Rayon = Annotated[float, Field(description="Rayon autour du point, en mètres", ge=10, le=3000)]


async def _sur(coro) -> dict:
    """Une erreur de source devient un résultat lisible plutôt qu'une exception : le modèle peut rebondir."""
    try:
        return await coro
    except SourceError as e:
        return {"etape": f"Échec · {e}", "erreur": str(e)}


@mcp.tool()
async def resoudre_territoire(requete: Annotated[str, Field(description="Nom de commune, code INSEE, code postal, n° de département ou SIREN d'EPCI")]) -> dict:
    """Identifie un territoire : code INSEE, population, surface, densité, EPCI, département, région, centre."""
    return await _sur(terr.resoudre_territoire(requete))


@mcp.tool()
async def geocoder(
    adresse: Annotated[str | None, Field(description="Adresse en texte libre")] = None,
    lon: Lon | None = None,
    lat: Lat | None = None,
    limite: Annotated[int, Field(ge=1, le=10)] = 1,
) -> dict:
    """Adresse -> coordonnées (WGS84 et Lambert-93) et code INSEE ; ou point -> adresse la plus proche (BAN)."""
    return await _sur(terr.geocoder(adresse, lon, lat, limite))


@mcp.tool()
async def parcelles(
    lon: Lon | None = None,
    lat: Lat | None = None,
    code_insee: CodeInsee | None = None,
    section: Annotated[str | None, Field(description="Section cadastrale, ex. AB")] = None,
    numero: Annotated[str | None, Field(description="Numéro de parcelle, ex. 82")] = None,
) -> dict:
    """Parcelles cadastrales sous un point ou par référence (commune, section, numéro) : idu et contenance."""
    return await _sur(foncier.parcelles(lon, lat, code_insee, section, numero))


@mcp.tool()
async def marche_immobilier(
    code_insee: CodeInsee,
    type_bien: Annotated[Literal["Appartement", "Maison"] | None, Field(description="Filtrer sur un type de bien")] = None,
    annees: Annotated[int, Field(description="Nombre d'années en arrière", ge=1, le=6)] = 5,
    lon: Lon | None = None,
    lat: Lat | None = None,
    rayon_m: Rayon = 500,
) -> dict:
    """Ventes DVF de logements : nombre, prix au m² (quartiles), prix et surface médians, évolution annuelle, part de VEFA.
    Avec lon/lat, se limite aux ventes dans le rayon donné."""
    return await _sur(foncier.marche_immobilier(code_insee, type_bien, annees, lon, lat, rayon_m))


@mcp.tool()
async def batiments(
    code_insee: CodeInsee | None = None,
    parcelle: Annotated[str | None, Field(description="Identifiant de parcelle (idu 14 caractères, ex. 69266000BB0392) pour la fiche détaillée")] = None,
    max_batiments: Annotated[int, Field(ge=100, le=50000)] = 20000,
) -> dict:
    """Parc bâti BDNB : usages, périodes de construction, logements, étiquettes DPE, énergie de chauffage, aléa argile.
    Avec `parcelle` : fiche détaillée de chaque bâtiment (adresse, année, matériaux, DPE, propriétaire personne morale)."""
    return await _sur(bati.batiments_bdnb(code_insee, parcelle, max_batiments))


@mcp.tool()
async def batiments_autour(lon: Lon, lat: Lat, rayon_m: Rayon = 200) -> dict:
    """Bâtiments BD TOPO autour d'un point : usages, nature, hauteurs, étages, logements, matériaux."""
    return await _sur(bati.batiments_bdtopo(lon, lat, rayon_m))


@mcp.tool()
async def dpe_logements(code_insee: CodeInsee) -> dict:
    """Diagnostics de performance énergétique (ADEME, depuis 2021) d'une commune : répartition A à G, part de passoires F-G."""
    return await _sur(bati.dpe(code_insee))


@mcp.tool()
async def urbanisme(lon: Lon, lat: Lat) -> dict:
    """Zonage PLU/PLUi ou carte communale, prescriptions, informations et servitudes d'utilité publique sur un point (GPU)."""
    return await _sur(regl.urbanisme(lon, lat))


@mcp.tool()
async def risques(code_insee: CodeInsee) -> dict:
    """Risques naturels et technologiques d'une commune : risques GASPAR, arrêtés CatNat, potentiel radon, zone de sismicité."""
    return await _sur(regl.risques(code_insee))


@mcp.tool()
async def lister_couches_ign(recherche: Annotated[str, Field(description="Mots-clés, ex. 'rpg', 'bdtopo route', 'ocsge'")]) -> dict:
    """Cherche une couche dans le catalogue WFS de la Géoplateforme IGN (BD TOPO, RPG, ADMIN EXPRESS, OCS GE, etc.)."""
    return await _sur(ign.lister_couches(recherche))


@mcp.tool()
async def interroger_couche_ign(
    couche: Annotated[str, Field(description="Nom technique de la couche, ex. BDTOPO_V3:troncon_de_route")],
    lon: Lon,
    lat: Lat,
    rayon_m: Rayon = 300,
    champs: Annotated[list[str] | None, Field(description="Attributs à résumer (tous par défaut)")] = None,
) -> dict:
    """Interroge n'importe quelle couche WFS IGN autour d'un point et résume ses attributs (répartitions, statistiques)."""
    return await _sur(ign.interroger_couche(couche, lon, lat, rayon_m, champs))


Limite = Annotated[int, Field(description="Nombre max d'entités renvoyées (page)", ge=1, le=5000)]
Offset = Annotated[int, Field(description="Décalage pour la page suivante (pagination.offset_suivant)", ge=0)]


@mcp.tool()
async def ventes_carte(
    code_insee: CodeInsee,
    type_bien: Annotated[Literal["Appartement", "Maison"] | None, Field(description="Filtrer sur un type de bien")] = None,
    neuf: Annotated[bool, Field(description="Seulement les ventes en VEFA (neuf sur plan)")] = False,
    annees: Annotated[int, Field(description="Nombre d'années en arrière", ge=1, le=6)] = 5,
    prix_m2_min: float | None = None,
    prix_m2_max: float | None = None,
    lon: Lon | None = None,
    lat: Lat | None = None,
    rayon_m: Rayon = 500,
    limite: Limite = 1000,
    offset: Offset = 0,
) -> dict:
    """Ventes DVF une par une, en COUCHE CARTE (points GeoJSON) : prix, surface, prix_m2, date, VEFA, adresse, parcelle.
    À utiliser pour afficher, classer (quantiles…) ou exporter des ventes. Le GeoJSON part sur la carte ;
    le modèle ne reçoit que le résumé, la pagination et la liste des champs."""
    return await _sur(foncier.ventes_carte(code_insee, type_bien, neuf, annees, prix_m2_min, prix_m2_max, lon, lat, rayon_m, limite, offset))


@mcp.tool()
async def entites_ign(
    couche: Annotated[str, Field(description="Nom technique WFS, ex. BDTOPO_V3:batiment, RPG.LATEST:parcelles_graphiques")],
    lon: Lon,
    lat: Lat,
    rayon_m: Rayon = 500,
    champs: Annotated[list[str] | None, Field(description="Attributs à garder (tous les attributs simples par défaut) ; leurs stats vont dans le résumé")] = None,
    champ_suggere: Annotated[str | None, Field(description="Attribut conseillé pour la symbologie, ex. hauteur")] = None,
    limite: Limite = 500,
    offset: Offset = 0,
) -> dict:
    """Objets d'une couche WFS IGN autour d'un point, en COUCHE CARTE (GeoJSON) : bâtiments, routes, parcelles agricoles…
    Le GeoJSON part sur la carte ; le modèle ne reçoit que le résumé et la pagination."""
    return await _sur(ign.entites(couche, lon, lat, rayon_m, champs, champ_suggere, limite, offset))


Bbox = Annotated[list[float] | None, Field(description="Emprise WGS84 [lon_min, lat_min, lon_max, lat_max] (300 km de côté max)", min_length=4, max_length=4)]
Lieu = Annotated[str | None, Field(description="Lieu dans le monde, à défaut de bbox (géocodage OpenStreetMap), ex. « Barrage des Trois-Gorges »")]
RayonKm = Annotated[float, Field(description="Demi-côté de la zone autour du lieu, en km", ge=0.5, le=150)]
TailleMax = Annotated[int, Field(description="Côté max du raster renvoyé, en pixels (la résolution s'adapte)", ge=64, le=1024)]


@mcp.tool()
async def catalogue_satellite(recherche: Annotated[str, Field(description="Mots-clés, ex. 'landsat', 'elevation', 'land cover', 'population'")]) -> dict:
    """Cherche une collection de données raster mondiales (satellite, MNT, occupation du sol, climat…) dans le catalogue STAC public."""
    return await _sur(sat.catalogue(recherche))


@mcp.tool()
async def rechercher_scenes(
    collection: Annotated[str, Field(description="Id de collection STAC, ex. landsat-c2-l2, sentinel-2-l2a")],
    debut: Annotated[str, Field(description="Date de début AAAA-MM-JJ")],
    fin: Annotated[str, Field(description="Date de fin AAAA-MM-JJ")],
    bbox: Bbox = None,
    lieu: Lieu = None,
    rayon_km: RayonKm = 15,
    nuages_max: Annotated[float | None, Field(description="Couverture nuageuse max de la scène, en %", ge=0, le=100)] = None,
) -> dict:
    """Liste les scènes disponibles sur une zone et une période : nombre par année, scènes les moins nuageuses, assets."""
    async def go():
        zone, _ = await sat.emprise(bbox, lieu, rayon_km)
        return await sat.scenes(collection, zone, debut, fin, nuages_max)
    return await _sur(go())


@mcp.tool(structured_output=False)  # GeoTIFF en base64 : une seule copie dans la réponse
async def composite_satellite(
    annee: Annotated[int, Field(description="Année visée", ge=1984, le=2100)],
    bbox: Bbox = None,
    lieu: Lieu = None,
    rayon_km: RayonKm = 15,
    capteur: Annotated[Literal["auto", "landsat", "sentinel-2"], Field(description="auto : Sentinel-2 (10 m) dès 2017, Landsat (30 m) avant")] = "auto",
    mois_debut: Annotated[int, Field(ge=1, le=12)] = 5,
    mois_fin: Annotated[int, Field(ge=1, le=12)] = 9,
    max_scenes: Annotated[int, Field(ge=1, le=15)] = 8,
    taille_max: TailleMax = 384,
    classer: Annotated[bool, Field(description="Ajoute des % indicatifs eau / forêt / végétation basse / bâti-sol nu")] = True,
    geotiff: Annotated[bool, Field(description="Renvoie le GeoTIFF 6 bandes (rouge, vert, bleu, pir, swir1, swir2) pour la carte")] = True,
    echantillons: Annotated[bool, Field(description="Propose des points d'entraînement par classe pour une classification supervisée, à relire sur l'image")] = False,
    par_classe: Annotated[int, Field(description="Points proposés par classe", ge=5, le=200)] = 40,
) -> dict:
    """Image satellite SANS NUAGES d'une année sur une zone du monde : nuages et ombres masqués pixel par pixel,
    médiane des meilleures scènes, fenêtre de dates élargie automatiquement si la zone reste nuageuse.
    Le GeoTIFF (clé geotiff_base64) part sur la carte ; le modèle ne reçoit que le résumé.
    Avec echantillons=True : `echantillons.geojson` = points d'entraînement proposés (classe, code), tirés d'une carte
    déjà classée et vérifiés sur l'image, à afficher sur l'image pour que l'utilisateur les valide avant de classer."""
    return await _sur(sat.composite(bbox, lieu, rayon_km, annee, capteur, mois_debut, mois_fin, max_scenes, taille_max, classer, geotiff,
                                    echantillons, par_classe))


@mcp.tool(structured_output=False)  # GeoTIFF en base64 : une seule copie dans la réponse
async def occupation_sol(
    annee: Annotated[int, Field(description="Année (1992–2023 selon le produit)", ge=1992, le=2100)],
    bbox: Bbox = None,
    lieu: Lieu = None,
    rayon_km: RayonKm = 15,
    produit: Annotated[Literal["auto", "io-lulc", "esa-worldcover", "esa-cci", "modis"],
                       Field(description="auto : IO/Esri 10 m (2017–2023), sinon ESA CCI 300 m (1992–2020), sinon MODIS 500 m")] = "auto",
    taille_max: TailleMax = 768,
    geotiff: bool = True,
) -> dict:
    """% d'occupation du sol (eau, forêt, cultures, bâti, sol nu…) d'une zone du monde, depuis des cartes déjà classées.
    Le GeoTIFF classé (clé geotiff_base64, avec légende et couleurs) part sur la carte."""
    return await _sur(sat.occupation_sol(bbox, lieu, rayon_km, annee, produit, taille_max, geotiff))


@mcp.tool(structured_output=False)  # GeoTIFF en base64 : une seule copie dans la réponse
async def evolution_occupation_sol(
    annees: Annotated[list[int], Field(description="Années à comparer, ex. [1995, 2000, 2005, 2010, 2015, 2020]", min_length=2, max_length=12)],
    bbox: Bbox = None,
    lieu: Lieu = None,
    rayon_km: RayonKm = 15,
    produit: Annotated[Literal["auto", "io-lulc", "esa-cci", "modis"], Field(description="auto : un seul produit si possible (ESA CCI pour 1992–2020)")] = "auto",
    taille_max: TailleMax = 512,
    couches: Annotated[bool, Field(description="Une couche classée par année (même grille, superposables) + une couche des changements, pour les afficher sur la carte")] = False,
) -> dict:
    """Tableau de l'occupation du sol (%) année par année, variation en points et principales transitions (ex. Cultures → Eau, en km²).
    Avec couches=True : `couches` = [{annee, raster, geotiff_base64}] sur une même grille (comparaison en rideau ou curseur d'années)
    et `changements.geotiff_base64` (seuls les pixels qui ont changé, colorés selon leur nouvelle classe)."""
    return await _sur(sat.evolution(bbox, lieu, rayon_km, annees, produit, taille_max, couches))


@mcp.tool(structured_output=False)  # GeoTIFF en base64 : une seule copie dans la réponse
async def extraire_raster(
    collection: Annotated[str, Field(description="Id de collection STAC, ex. cop-dem-glo-30 (altitude)")],
    asset: Annotated[str, Field(description="Nom de l'asset, ex. data, B04, elevation")],
    debut: Annotated[str, Field(description="AAAA-MM-JJ")] = "1900-01-01",
    fin: Annotated[str, Field(description="AAAA-MM-JJ")] = "2100-12-31",
    bbox: Bbox = None,
    lieu: Lieu = None,
    rayon_km: RayonKm = 15,
    resolution_native_m: Annotated[float, Field(description="Résolution native de la donnée, en m", gt=0)] = 30,
    taille_max: TailleMax = 512,
) -> dict:
    """Extrait n'importe quel raster du catalogue STAC sur une zone (mosaïque des tuiles, 1 bande) : MNT, population, climat…
    Le GeoTIFF (clé geotiff_base64) part sur la carte ; le modèle reçoit min, médiane et max."""
    return await _sur(sat.extraire(collection, asset, bbox, lieu, rayon_km, debut, fin, taille_max, resolution_native_m))


@mcp.tool(structured_output=False)  # tuiles JPEG en base64 : pour l'affichage de la page, pas pour le modèle
async def fond_satellite(
    tuiles: Annotated[list[str], Field(description="Tuiles Web Mercator 'z/x/y' (schéma XYZ), 24 au plus par appel", min_length=1, max_length=64)],
    source: Annotated[Literal["auto", "ign", "eox"], Field(description="auto : orthophoto IGN en France, Sentinel-2 cloudless ailleurs")] = "auto",
) -> dict:
    """Fond de carte satellite de la page : renvoie les tuiles demandées en JPEG base64 (clé tuiles[].jpeg_base64).
    Réservé à l'affichage de la carte ; le modèle n'a pas à l'appeler."""
    try:
        return await fnd.fond(tuiles, source)
    except ValueError as e:
        return {"etape": f"Échec, {e}", "erreur": str(e)}


CATALOGUE = {
    "live_sans_cle": {
        "Découpage administratif / COG": "resoudre_territoire",
        "Base Adresse Nationale": "geocoder",
        "Cadastre PCI": "parcelles",
        "DVF géolocalisées (5 ans)": "marche_immobilier, ventes_carte (couche)",
        "BDNB": "batiments",
        "BD TOPO": "batiments_autour, interroger_couche_ign",
        "DPE ADEME": "dpe_logements",
        "Géoportail de l'Urbanisme": "urbanisme",
        "Géorisques": "risques",
        "Tout le WFS IGN (RPG, OCS GE, ADMIN EXPRESS...)": "lister_couches_ign, interroger_couche_ign, entites_ign (couche)",
    },
    "monde_satellite": {
        "Landsat 4-9 (1984+, 30 m) et Sentinel-2 (2017+, 10 m), sans nuages": "composite_satellite",
        "Occupation du sol ESA CCI, MODIS, IO/Esri, ESA WorldCover": "occupation_sol, evolution_occupation_sol",
        "Tout le catalogue Planetary Computer (MNT, climat, population...)": "catalogue_satellite, rechercher_scenes, extraire_raster",
        "Fond satellite de la carte (ortho IGN, Sentinel-2 cloudless)": "fond_satellite",
    },
    "a_venir": {
        "Carreaux Filosofi 200 m": "ingestion PostGIS (fichiers seulement)",
        "Recensement INSEE détaillé": "API Melodi INSEE",
        "SIRENE géolocalisée": "ingestion du stock",
        "Requêtes SQL expertes": "requete_sql sur PostGIS",
    },
}


@mcp.resource("geosphere://catalogue", name="catalogue", mime_type="application/json")
def catalogue() -> str:
    """Sources branchées et outils correspondants."""
    return json.dumps(CATALOGUE, ensure_ascii=False, indent=1)


@mcp.custom_route("/", methods=["GET"])
async def sante(_request):
    """Page d'accueil et sonde de santé de l'hébergeur."""
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("Géosphère MCP : point d'accès /mcp")


def main() -> None:
    if "--http" in sys.argv:
        # En ligne (Render…) : écoute sur toutes les interfaces et le port fourni par l'hébergeur.
        # Sans état de session, pour survivre aux mises en veille et redémarrages.
        mcp.run("streamable-http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), stateless_http=True)
    else:
        mcp.run("stdio")
