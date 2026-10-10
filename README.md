# Géosphère MCP

Serveur MCP qui donne à Géosphère (et à Claude Desktop, Claude Code ou claude.ai) un accès en direct aux données SIG françaises. Chaque outil renvoie un résumé compact, une ligne `etape` à afficher dans le fil du chatbot, et la `source` à citer.

## Outils (v0.2, sans base de données ni clé)

| Outil | Ce qu'il renvoie | Source |
| --- | --- | --- |
| `resoudre_territoire` | Commune, EPCI, département ou code postal : codes, population, densité, rattachements | API Découpage administratif (COG) |
| `geocoder` | Adresse vers coordonnées WGS84 + Lambert-93 + code INSEE, et l'inverse | BAN via Géoplateforme |
| `parcelles` | Parcelles sous un point ou par référence : idu, contenance | Cadastre PCI via API Carto |
| `marche_immobilier` | Ventes DVF de logements : prix au m² (quartiles), évolution annuelle, VEFA ; filtre par rayon | DVF géolocalisées |
| `batiments` | Parc bâti d'une commune (usages, périodes, DPE, chauffage, argile) ou fiche des bâtiments d'une parcelle | BDNB |
| `batiments_autour` | Bâtiments autour d'un point : usages, hauteurs, étages, logements | BD TOPO (WFS) |
| `dpe_logements` | Répartition des étiquettes A à G, part de passoires F-G | DPE ADEME |
| `urbanisme` | Zonage PLU/PLUi ou carte communale, prescriptions, SUP, RNU | Géoportail de l'Urbanisme |
| `risques` | Risques GASPAR, arrêtés CatNat, radon, sismicité | Géorisques |
| `lister_couches_ign` / `interroger_couche_ign` | N'importe quelle couche WFS IGN (RPG, OCS GE, routes, ADMIN EXPRESS...) résumée autour d'un point | Géoplateforme IGN |
| `ventes_carte` | **Couche carte** : ventes DVF une par une en points GeoJSON (prix, surface, prix_m2, VEFA, adresse), filtres et pagination | DVF géolocalisées |
| `entites_ign` | **Couche carte** : objets d'une couche WFS IGN autour d'un point en GeoJSON (bâtiments, routes, RPG...) | Géoplateforme IGN |

Les outils « couche carte » renvoient un résumé court (`etape`, `couche`, `pagination`, `resume`) et une clé `geojson`. Le chatbot charge `geojson` comme couche puis la retire avant de passer le résultat au modèle : la carte reçoit les entités, le modèle seulement le résumé. Page par défaut : 1 000 ventes (~340 Ko) ou 500 objets IGN ; `offset` = `pagination.offset_suivant` pour la suite.

La ressource `geosphere://catalogue` liste les sources branchées et celles à venir (Filosofi, recensement détaillé, SIRENE, SQL PostGIS).

## Installation

```bash
cd geosphere-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest -m live -s        # appelle les vraies API et affiche une ligne par outil
```

### Brancher dans Claude Desktop

Dans `claude_desktop_config.json` (Réglages > Développeur > Modifier la config) :

```json
{
  "mcpServers": {
    "geosphere": {
      "command": "/chemin/vers/geosphere-mcp/.venv/bin/python",
      "args": ["-m", "geosphere_mcp"],
      "cwd": "/chemin/vers/geosphere-mcp"
    }
  }
}
```

Redémarrer Claude Desktop, puis demander par exemple : « Prix au m² des appartements à Villeurbanne depuis 2021, et zonage PLU au 30 rue Jean Bourgey ».

### En serveur HTTP (backend Géosphère, connecteur claude.ai)

```bash
python -m geosphere_mcp --http   # http://localhost:8000/mcp
```

## Tests

- `pytest -m "not live"` : chaque outil appelé via le protocole MCP contre des réponses enregistrées (forme réelle relevée sur Villeurbanne le 2026-10-09).
- `pytest -m live` : mêmes outils contre les API publiques.

## Limites connues

- DVF : Paris, Lyon et Marseille se requêtent par arrondissement ; Alsace-Moselle et Mayotte ne sont pas dans DVF.
- BDNB : l'API open data est plafonnée en débit ; au-delà de `max_batiments` le résultat est marqué `echantillon`.
- WFS IGN : 5 000 objets maximum par requête, signalé par `tronque`.
