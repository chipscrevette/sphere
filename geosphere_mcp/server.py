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

from . import batiments as bati, foncier, ign, reglementaire as regl, territoire as terr
from .http import SourceError

INSTRUCTIONS = """Données SIG françaises en direct (IGN, INSEE/COG, BAN, cadastre, DVF, BDNB, DPE ADEME, GPU, Géorisques).
Commencer par resoudre_territoire ou geocoder pour obtenir code INSEE et coordonnées, puis appeler l'outil métier.
Chaque réponse porte `etape` (ligne à afficher dans le fil d'étapes) et `source` (à citer avec le millésime).
Les résultats sont déjà agrégés : ne pas redemander le détail brut sauf besoin explicite."""

mcp = MCPServer("geosphere", title="Géosphère · données SIG", instructions=INSTRUCTIONS, version="0.1.0")

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


CATALOGUE = {
    "live_sans_cle": {
        "Découpage administratif / COG": "resoudre_territoire",
        "Base Adresse Nationale": "geocoder",
        "Cadastre PCI": "parcelles",
        "DVF géolocalisées (5 ans)": "marche_immobilier",
        "BDNB": "batiments",
        "BD TOPO": "batiments_autour, interroger_couche_ign",
        "DPE ADEME": "dpe_logements",
        "Géoportail de l'Urbanisme": "urbanisme",
        "Géorisques": "risques",
        "Tout le WFS IGN (RPG, OCS GE, ADMIN EXPRESS...)": "lister_couches_ign, interroger_couche_ign",
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
