"""
MCP Server for querying building damage data.

Auto-discovers the damage dataset (shapefile, GeoJSON, or GeoPackage) via:
  1. DAMAGE_DATA_PATH  – environment variable pointing directly to the file.
  2. data_config.json  – written by the damage-assessment pipeline; contains
                         {"data_path": "/abs/path/to/file"}.
  3. Directory search  – looks for the most recently modified geospatial file
                         in ./data/, ./output/, ./results/, and the server
                         directory itself (non-recursive + one level deep).

Exposes tools for geocoding, spatial queries, and damage statistics.
"""

import json
import logging
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd
from geopy.geocoders import Nominatim
from mcp.server.fastmcp import FastMCP
from shapely.geometry import box, Point

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Data discovery ----------

GEOSPATIAL_EXTENSIONS = (".shp", ".geojson", ".gpkg", ".json")
SERVER_DIR = Path(__file__).parent.resolve()

# Directories searched in order; each is searched non-recursively and one
# level deep to avoid slow filesystem traversal.
_SEARCH_DIRS = [
    SERVER_DIR / "data",
    SERVER_DIR / "output",
    SERVER_DIR / "results",
    SERVER_DIR,
    SERVER_DIR.parent / "data",
    SERVER_DIR.parent / "output",
    SERVER_DIR.parent / "results",
]


def _find_data_file() -> Path:
    """Return the path to the damage dataset using the discovery chain."""

    # 1. Explicit environment variable
    env_path = os.environ.get("DAMAGE_DATA_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            logger.info("Using DAMAGE_DATA_PATH: %s", p)
            return p
        raise FileNotFoundError(
            f"DAMAGE_DATA_PATH is set but the file was not found: {p}"
        )

    # 2. Config file written by the damage-assessment pipeline
    config_path = SERVER_DIR / "data_config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            if "data_path" in cfg:
                p = Path(cfg["data_path"])
                if p.exists():
                    logger.info("Using path from data_config.json: %s", p)
                    return p
                logger.warning(
                    "data_config.json points to non-existent file: %s", p
                )
        except Exception as exc:
            logger.warning("Could not read data_config.json: %s", exc)

    # 3. Search candidate directories for geospatial files
    candidates: list[Path] = []
    for d in _SEARCH_DIRS:
        if not d.is_dir():
            continue
        for ext in GEOSPATIAL_EXTENSIONS:
            # Direct children
            candidates.extend(d.glob(f"*{ext}"))
            # One subdirectory deep (covers the shapefile-in-own-folder pattern)
            candidates.extend(d.glob(f"*/*{ext}"))

    # Skip GeoJSON-like .json files that are obviously not geodata
    candidates = [
        p for p in candidates
        if p.suffix in (".shp", ".gpkg", ".geojson")
        or (p.suffix == ".json" and "geo" in p.stem.lower())
    ]

    if not candidates:
        raise FileNotFoundError(
            "No geospatial damage data file found.\n"
            "Options:\n"
            "  • Set DAMAGE_DATA_PATH=/path/to/file.shp (or .geojson/.gpkg)\n"
            "  • Create data_config.json with {\"data_path\": \"/path/to/file\"}\n"
            "  • Place the file under ./data/, ./output/, or ./results/"
        )

    # Pick the most recently modified file
    best = max(candidates, key=lambda p: p.stat().st_mtime)
    if len(candidates) > 1:
        logger.info(
            "Found %d candidate files; selecting most recently modified: %s",
            len(candidates),
            best,
        )
    else:
        logger.info("Auto-discovered data file: %s", best)
    return best


# ---------- Data loading ----------

_data_path = _find_data_file()
logger.info("Loading: %s", _data_path)
GDF = gpd.read_file(_data_path)
GDF = GDF.to_crs(epsg=4326)
SINDEX = GDF.sindex

_bounds = GDF.total_bounds  # [minx, miny, maxx, maxy]
logger.info(
    "Loaded %d features. Bounds: (%.4f, %.4f) – (%.4f, %.4f). Spatial index built.",
    len(GDF),
    _bounds[0], _bounds[1],
    _bounds[2], _bounds[3],
)

# ---------- Geocoder ----------

GEOCODER = Nominatim(user_agent="building_damage_mcp_server")

# ---------- MCP Server ----------

# Build a dynamic description from the loaded data
_damage_vals = (
    sorted(GDF["DAMAGE"].dropna().unique().tolist())
    if "DAMAGE" in GDF.columns else []
)
_instructions = (
    f"This server provides tools to query a building damage polygon dataset "
    f"loaded from '{_data_path.name}'. "
    f"The dataset contains {len(GDF):,} building footprint polygons "
    f"covering the area bounded by "
    f"({_bounds[1]:.3f}°–{_bounds[3]:.3f}°N latitude, "
    f"{_bounds[0]:.3f}°–{_bounds[2]:.3f}° longitude). "
    + (f"Damage categories present: {', '.join(_damage_vals)}. " if _damage_vals else "")
    + "Use geocode_place to resolve a place name to coordinates, then use "
    "query_damage_in_area or query_damage_in_radius to find buildings and their damage status. "
    "Use get_damage_summary for overall statistics, and list_fields to explore the data schema."
)

mcp = FastMCP("building-damage", instructions=_instructions)


# ---------- Tools ----------

@mcp.tool()
def list_fields() -> str:
    """List all available fields in the building damage dataset with their types and sample values.
    Use this first to understand what data is available before querying."""
    result = {}
    for col in GDF.columns:
        if col == "geometry":
            continue
        info = {"dtype": str(GDF[col].dtype), "non_null_count": int(GDF[col].notna().sum())}
        nuniq = GDF[col].nunique()
        info["unique_count"] = nuniq
        if nuniq <= 30:
            info["unique_values"] = sorted(
                [str(v) for v in GDF[col].dropna().unique().tolist()]
            )
        else:
            info["sample_values"] = [
                str(v) for v in GDF[col].dropna().head(5).tolist()
            ]
        result[col] = info
    return json.dumps(result, indent=2)


@mcp.tool()
def get_damage_summary(
    damage_category: str | None = None,
    structure_type: str | None = None,
) -> str:
    """Get summary statistics for the entire damage dataset.

    Args:
        damage_category: Optional filter by damage category substring
                         (e.g. 'Destroyed', 'No Damage'). If None, returns all categories.
        structure_type: Optional filter by structure type substring
                        (e.g. 'Single Family Residence').
    """
    subset = GDF.copy()
    if structure_type and "STRUCTURET" in subset.columns:
        subset = subset[subset["STRUCTURET"].str.contains(structure_type, case=False, na=False)]
    if damage_category and "DAMAGE" in subset.columns:
        subset = subset[subset["DAMAGE"].str.contains(damage_category, case=False, na=False)]

    result: dict = {"total_buildings": len(subset)}
    if "DAMAGE" in subset.columns:
        damage_counts = subset["DAMAGE"].value_counts(dropna=False).to_dict()
        result["damage_breakdown"] = {
            (str(k) if pd.notna(k) else "Unassessed"): v
            for k, v in damage_counts.items()
        }
    if "STRUCTURET" in subset.columns:
        struct_counts = subset["STRUCTURET"].value_counts(dropna=False).to_dict()
        result["structure_type_breakdown"] = {
            (str(k) if pd.notna(k) else "Unknown"): v
            for k, v in struct_counts.items()
        }
    return json.dumps(result, indent=2)


@mcp.tool()
def geocode_place(place_name: str, context: str = "") -> str:
    """Geocode a place name to get its coordinates and bounding box.

    Use this to resolve names like 'The Huntington', 'Palisades Village', etc.
    Returns lat/lon and a bounding box that can be used with query_damage_in_area.

    Args:
        place_name: The name of the place to geocode (e.g. 'Palisades Charter High School').
        context: Geographic context to help disambiguate, e.g. 'Altadena, CA'.
                 If empty, the search is biased to the bounding box of the loaded dataset.
    """
    # Bias geocoding to the area covered by the loaded data
    data_bounds = GDF.total_bounds  # [minx, miny, maxx, maxy]
    viewbox = (
        (data_bounds[1] - 0.1, data_bounds[0] - 0.1),
        (data_bounds[3] + 0.1, data_bounds[2] + 0.1),
    )

    query = f"{place_name}, {context}" if context else place_name
    location = GEOCODER.geocode(
        query, exactly_one=True, addressdetails=True, timeout=10,
        viewbox=viewbox, bounded=True,
    )
    if location is None:
        location = GEOCODER.geocode(
            query, exactly_one=True, addressdetails=True, timeout=10,
            viewbox=viewbox, bounded=False,
        )
    if location is None and context:
        location = GEOCODER.geocode(
            place_name, exactly_one=True, addressdetails=True, timeout=10,
            viewbox=viewbox, bounded=False,
        )
    if location is None:
        return json.dumps({
            "error": (
                f"Could not geocode '{place_name}'. "
                "Try a more specific name or add a context argument."
            )
        })

    result: dict = {
        "display_name": location.address,
        "latitude": location.latitude,
        "longitude": location.longitude,
    }
    if hasattr(location, "raw") and "boundingbox" in location.raw:
        bb = location.raw["boundingbox"]
        result["bounding_box"] = {
            "south": float(bb[0]),
            "north": float(bb[1]),
            "west": float(bb[2]),
            "east": float(bb[3]),
        }
    return json.dumps(result, indent=2)


@mcp.tool()
def query_damage_in_area(
    south: float,
    north: float,
    west: float,
    east: float,
    damage_category: str | None = None,
    structure_type: str | None = None,
) -> str:
    """Query building damage within a bounding box area.

    Use the bounding box from geocode_place, or specify your own coordinates.

    Args:
        south: Southern latitude boundary.
        north: Northern latitude boundary.
        west: Western longitude boundary.
        east: Eastern longitude boundary.
        damage_category: Optional damage filter substring (e.g. 'Destroyed').
        structure_type: Optional structure type filter substring (e.g. 'Single Family').
    """
    bbox = box(west, south, east, north)
    candidate_idx = list(SINDEX.intersection(bbox.bounds))
    candidates = GDF.iloc[candidate_idx]
    hits = candidates[candidates.intersects(bbox)]

    if structure_type and "STRUCTURET" in hits.columns:
        hits = hits[hits["STRUCTURET"].str.contains(structure_type, case=False, na=False)]
    if damage_category and "DAMAGE" in hits.columns:
        hits = hits[hits["DAMAGE"].str.contains(damage_category, case=False, na=False)]

    result: dict = {
        "total_buildings_in_area": len(hits),
        "bounding_box_used": {"south": south, "north": north, "west": west, "east": east},
    }
    if "DAMAGE" in hits.columns:
        damage_counts = hits["DAMAGE"].value_counts(dropna=False).to_dict()
        result["damage_breakdown"] = {
            (str(k) if pd.notna(k) else "Unassessed"): v
            for k, v in damage_counts.items()
        }
    if "STRUCTURET" in hits.columns:
        struct_counts = hits["STRUCTURET"].value_counts(dropna=False).to_dict()
        result["structure_type_breakdown"] = {
            (str(k) if pd.notna(k) else "Unknown"): v
            for k, v in struct_counts.items()
        }
    return json.dumps(result, indent=2)


@mcp.tool()
def query_damage_in_radius(
    latitude: float,
    longitude: float,
    radius_meters: float = 500.0,
    damage_category: str | None = None,
    structure_type: str | None = None,
) -> str:
    """Query building damage within a radius of a point.

    Args:
        latitude: Center latitude (WGS84).
        longitude: Center longitude (WGS84).
        radius_meters: Search radius in meters (default 500m).
        damage_category: Optional damage filter substring (e.g. 'Destroyed').
        structure_type: Optional structure type filter substring.
    """
    center = Point(longitude, latitude)
    gdf_proj = GDF.to_crs(epsg=3857)
    center_proj = gpd.GeoSeries([center], crs="EPSG:4326").to_crs(epsg=3857).iloc[0]
    buffer = center_proj.buffer(radius_meters)

    candidate_idx = list(gdf_proj.sindex.intersection(buffer.bounds))
    candidates = gdf_proj.iloc[candidate_idx]
    hits_proj = candidates[candidates.intersects(buffer)]
    hits = GDF.loc[hits_proj.index]

    if structure_type and "STRUCTURET" in hits.columns:
        hits = hits[hits["STRUCTURET"].str.contains(structure_type, case=False, na=False)]
    if damage_category and "DAMAGE" in hits.columns:
        hits = hits[hits["DAMAGE"].str.contains(damage_category, case=False, na=False)]

    result: dict = {
        "total_buildings_in_radius": len(hits),
        "center": {"latitude": latitude, "longitude": longitude},
        "radius_meters": radius_meters,
    }
    if "DAMAGE" in hits.columns:
        damage_counts = hits["DAMAGE"].value_counts(dropna=False).to_dict()
        result["damage_breakdown"] = {
            (str(k) if pd.notna(k) else "Unassessed"): v
            for k, v in damage_counts.items()
        }
    if "STRUCTURET" in hits.columns:
        struct_counts = hits["STRUCTURET"].value_counts(dropna=False).to_dict()
        result["structure_type_breakdown"] = {
            (str(k) if pd.notna(k) else "Unknown"): v
            for k, v in struct_counts.items()
        }
    return json.dumps(result, indent=2)


@mcp.tool()
def query_buildings_detail(
    south: float,
    north: float,
    west: float,
    east: float,
    damage_category: str | None = None,
    structure_type: str | None = None,
    limit: int = 50,
) -> str:
    """Get detailed info for individual buildings in a bounding box.

    Returns per-building attributes (no geometry) for inspection.

    Args:
        south: Southern latitude boundary.
        north: Northern latitude boundary.
        west: Western longitude boundary.
        east: Eastern longitude boundary.
        damage_category: Optional damage filter substring.
        structure_type: Optional structure type filter substring.
        limit: Max number of buildings to return (default 50).
    """
    bbox = box(west, south, east, north)
    candidate_idx = list(SINDEX.intersection(bbox.bounds))
    candidates = GDF.iloc[candidate_idx]
    hits = candidates[candidates.intersects(bbox)]

    if structure_type and "STRUCTURET" in hits.columns:
        hits = hits[hits["STRUCTURET"].str.contains(structure_type, case=False, na=False)]
    if damage_category and "DAMAGE" in hits.columns:
        hits = hits[hits["DAMAGE"].str.contains(damage_category, case=False, na=False)]

    # Return whatever attribute columns are present (skip geometry)
    cols = [c for c in hits.columns if c != "geometry"]
    subset = hits[cols].head(limit)

    records = subset.fillna("N/A").to_dict(orient="records")
    result = {
        "total_matching": len(hits),
        "returned": len(records),
        "buildings": records,
    }
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
