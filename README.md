# Building Damage Query MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that lets AI assistants query geospatial building damage data through natural language. Point it at a shapefile, GeoJSON, or GeoPackage and ask questions like *"How many homes were destroyed near Altadena High School?"*

![Building Damage Query Demo](demo/Building_Damage_Query.gif)

---

## Overview

```
Your damage dataset (.shp / .geojson / .gpkg)
        │
        ▼
  server.py  (FastMCP server)
        │  auto-discovers the file on startup
        │  loads it into GeoPandas with a spatial index 
        │  
        ▼
  MCP Tools
  ┌──────────────────────────┐
  │  geocode_place           │
  │  get_damage_summary      │
  │  query_damage_in_area    │
  │  query_damage_in_radius  │
  │  query_buildings_detail  │
  │  list_fields             │
  └──────────────────────────┘
        │
        ▼
  MCP client ── asks natural-language questions
```

The server sits between your geospatial data and an AI assistant. Claude calls the tools automatically based on your question — you never write code or run queries yourself.

---

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) package manager
- A geospatial damage dataset (`.shp`, `.geojson`, or `.gpkg`)

### Python dependencies

| Package | Purpose |
|---------|---------|
| `geopandas` | Loads and spatially indexes the dataset |
| `shapely` | Geometry operations (bounding box, buffer) |
| `pyproj` | Coordinate reference system transforms |
| `fiona` | Shapefile I/O backend |
| `geopy` | Place-name geocoding via Nominatim/OpenStreetMap |
| `mcp[cli]` | FastMCP server framework |

---

## Installation

```bash
git clone https://github.com/your-org/building-damage-mcp
cd building-damage-mcp
uv sync
```

---

## Data Setup

The server discovers your dataset automatically using this priority chain:

### Priority 1 — Environment variable (highest)

Set `DAMAGE_DATA_PATH` to the absolute path of your file:

```bash
export DAMAGE_DATA_PATH=/path/to/damage.shp
uv run python server.py
```

Or configure it in `.mcp.json` (see [Claude Code integration](#claude-code-integration) below).

### Priority 2 — `data_config.json`

Create a `data_config.json` file in the server directory:

```json
{ "data_path": "/absolute/path/to/damage.geojson" }
```

This is ideal for pipeline integration — your damage-assessment script writes this file after producing output, and the server picks it up on next startup.

### Priority 3 — Directory search (automatic)

Place your data file in any of these locations and the server will find the most recently modified one automatically:

```
./data/
./output/
./results/
./          (server directory itself)
../data/
../output/
../results/
```

---

## Expected Data Schema

The tools adapt to whatever columns are present, but work best with:

| Column | Description | Example values |
|--------|-------------|----------------|
| `DAMAGE` | Damage category per building | `Destroyed (>50%)`, `Major (26-50%)`, `Minor (10-25%)`, `Affected (1-9%)`, `No Damage`, `Inaccessible` |
| `STRUCTURET` | Structure type | `Single Family Residence`, `Commercial`, `Multi-Family Dwelling` |
| `HEIGHT` | Building height | `12.5` |
| `AREA` | Footprint area (m²) | `185.3` |
| `ELEV` | Elevation | `342.1` |

All columns are optional — use `list_fields` at runtime to see what your dataset contains.

---

## MCP Tools Reference

### `list_fields`

Lists every column in the dataset with its data type, non-null count, and unique values (or a sample if there are many).

**Use this first** when working with an unfamiliar dataset.

**Parameters:** none

**Example response:**
```json
{
  "DAMAGE": {
    "dtype": "object",
    "non_null_count": 47894,
    "unique_count": 6,
    "unique_values": ["Affected (1-9%)", "Destroyed (>50%)", "Inaccessible", "Major (26-50%)", "Minor (10-25%)", "No Damage"]
  },
  "STRUCTURET": {
    "dtype": "object",
    "non_null_count": 47890,
    "unique_count": 12,
    "unique_values": ["Commercial", "Multi-Family Dwelling", "Single Family Residence", "..."]
  }
}
```

---

### `get_damage_summary`

Returns aggregate statistics across the **entire dataset**, optionally filtered.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `damage_category` | `str \| None` | Filter by damage category substring (e.g. `"Destroyed"`) |
| `structure_type` | `str \| None` | Filter by structure type substring (e.g. `"Single Family"`) |

**Example — all buildings:**
```json
{
  "total_buildings": 47894,
  "damage_breakdown": {
    "No Damage": 31240,
    "Affected (1-9%)": 5102,
    "Minor (10-25%)": 3841,
    "Major (26-50%)": 2987,
    "Destroyed (>50%)": 4512,
    "Inaccessible": 212
  },
  "structure_type_breakdown": {
    "Single Family Residence": 38100,
    "Commercial": 4200
  }
}
```

---

### `geocode_place`

Resolves a place name to latitude/longitude and a bounding box. Uses OpenStreetMap/Nominatim, biased to the geographic extent of the loaded dataset.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `place_name` | `str` | Name to geocode (e.g. `"Palisades Charter High School"`) |
| `context` | `str` | Geographic context for disambiguation (e.g. `"Pacific Palisades, CA"`) |

**Example:**
```json
{
  "display_name": "The Huntington Library, Art Museum, and Botanical Gardens, Allen Ave, San Marino, CA",
  "latitude": 34.1290,
  "longitude": -118.1143,
  "bounding_box": {
    "south": 34.1241,
    "north": 34.1339,
    "west": -118.1210,
    "east": -118.1076
  }
}
```

Feed the `bounding_box` directly into `query_damage_in_area`, or use `latitude`/`longitude` with `query_damage_in_radius`.

---

### `query_damage_in_area`

Queries damage statistics for all buildings that intersect a bounding box.

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `south` | `float` | Southern latitude boundary |
| `north` | `float` | Northern latitude boundary |
| `west` | `float` | Western longitude boundary |
| `east` | `float` | Eastern longitude boundary |
| `damage_category` | `str \| None` | Optional filter substring |
| `structure_type` | `str \| None` | Optional filter substring |

**Example:**
```json
{
  "total_buildings_in_area": 843,
  "bounding_box_used": { "south": 34.124, "north": 34.134, "west": -118.121, "east": -118.108 },
  "damage_breakdown": {
    "Destroyed (>50%)": 312,
    "Major (26-50%)": 98,
    "Minor (10-25%)": 73,
    "No Damage": 360
  }
}
```

---

### `query_damage_in_radius`

Queries damage statistics for all buildings within a circle defined by a center point and radius.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `latitude` | `float` | — | Center latitude (WGS84) |
| `longitude` | `float` | — | Center longitude (WGS84) |
| `radius_meters` | `float` | `500.0` | Search radius in meters |
| `damage_category` | `str \| None` | `None` | Optional filter substring |
| `structure_type` | `str \| None` | `None` | Optional filter substring |

**Example:**
```json
{
  "total_buildings_in_radius": 214,
  "center": { "latitude": 34.1290, "longitude": -118.1143 },
  "radius_meters": 1000.0,
  "damage_breakdown": {
    "No Damage": 142,
    "Affected (1-9%)": 31,
    "Destroyed (>50%)": 41
  }
}
```

---

### `query_buildings_detail`

Returns per-building attribute rows (no geometry) within a bounding box. Useful for inspecting individual buildings rather than aggregated statistics.

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `south` | `float` | — | Southern latitude boundary |
| `north` | `float` | — | Northern latitude boundary |
| `west` | `float` | — | Western longitude boundary |
| `east` | `float` | — | Eastern longitude boundary |
| `damage_category` | `str \| None` | `None` | Optional filter substring |
| `structure_type` | `str \| None` | `None` | Optional filter substring |
| `limit` | `int` | `50` | Max buildings to return |

**Example:**
```json
{
  "total_matching": 312,
  "returned": 50,
  "buildings": [
    { "DAMAGE": "Destroyed (>50%)", "STRUCTURET": "Single Family Residence", "HEIGHT": 7.2, "AREA": 148.0 },
    { "DAMAGE": "Major (26-50%)", "STRUCTURET": "Commercial", "HEIGHT": 12.1, "AREA": 530.0 }
  ]
}
```

---

## Typical Query Workflow

For most location-based questions, Claude follows this pattern automatically:

```
User: "How many homes were destroyed near Altadena High School?"

  Step 1 → geocode_place("Altadena High School", "Altadena, CA")
             Returns: lat=34.189, lon=-118.131, bounding_box={...}

  Step 2 → query_damage_in_radius(34.189, -118.131, radius_meters=1000,
                                   damage_category="Destroyed",
                                   structure_type="Single Family")
             Returns: { total_buildings_in_radius: 87, damage_breakdown: {...} }

  Step 3 → Claude reports: "Within 1 km of Altadena High School,
             1039 residential buildings were destroyed."
```

For dataset-wide questions (no location), Claude calls `get_damage_summary` directly. To explore an unfamiliar dataset, it starts with `list_fields`.

---

## Claude Code Integration

Add the server to your `.mcp.json` so Claude Code connects to it automatically:

```json
{
  "mcpServers": {
    "building-damage": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/building-damage-mcp", "python", "server.py"],
      "env": {
        "DAMAGE_DATA_PATH": "/path/to/your/damage_data.shp"
      }
    }
  }
}
```

Place this file in your project root or in `~/.claude/` for global access. Once connected, Claude can answer damage queries directly in conversation with no additional setup.

---

## Pipeline Integration

If you have a damage-assessment model that produces output files, integrate it like this:

```python
# run_damage_model.py (your pipeline script)
import json
from pathlib import Path

output_file = Path("output/damage.geojson")
run_model(output=output_file)  # your model writes the geospatial file

# Tell the MCP server where to find the new output on next start
Path("data_config.json").write_text(
    json.dumps({"data_path": str(output_file.resolve())})
)
```

The server reads `data_config.json` at startup, so restart it after a new model run to load fresh results. No code changes required.

---

## Running the Server Manually

```bash
# With auto-discovery (file in ./data/, ./output/, or ./results/)
uv run python server.py

# With an explicit path
DAMAGE_DATA_PATH=/path/to/damage.shp uv run python server.py
```

On startup the server logs the file it loaded, feature count, and geographic bounds:

```
INFO  Auto-discovered data file: output/damage.geojson
INFO  Loading: output/damage.geojson
INFO  Loaded 47894 features. Bounds: (-118.688, 34.029) – (-118.018, 34.232). Spatial index built.
```

---

## Example Conversations

**Neighborhood-level summary**
> "Give me a breakdown of all damage in Pacific Palisades."

Claude calls `geocode_place("Pacific Palisades", "CA")` → `query_damage_in_area(...)` and returns a damage table.

**Radius search around a landmark**
> "How many buildings within 500 meters of the Getty Villa were destroyed?"

Claude calls `geocode_place("Getty Villa")` → `query_damage_in_radius(..., radius_meters=500, damage_category="Destroyed")`.

**Dataset-wide statistics**
> "What percentage of commercial buildings had major or worse damage?"

Claude calls `get_damage_summary(structure_type="Commercial")` for the total, then `get_damage_summary(structure_type="Commercial", damage_category="Major")` for the filtered count, and computes the percentage.

**Exploring an unknown dataset**
> "What data do you have about these buildings?"

Claude calls `list_fields()` and summarizes the available columns and their values.

**Per-building inspection**
> "Show me details on destroyed buildings near Lake Avenue."

Claude calls `geocode_place` → `query_buildings_detail(..., damage_category="Destroyed", limit=20)` and presents individual rows.
