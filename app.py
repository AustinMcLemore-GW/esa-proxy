"""
Phase I ESA Database Proxy — v9.101
FUDS envelope query + dedup, ERIC layer 8 integration, responsible party → voluntary cleanup.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, math, time, json, os

# ── FUDS static data (FY2024) ─────────────────────────────────────────────────
# USACE ArcGIS servers block automated requests from cloud hosting IPs.
# Data loaded from local file downloaded from USACE open data portal.
# ⚠️  UPDATE REMINDER: Download new data each October when USACE releases
#     the next fiscal year. Source:
#     https://geospatial-usace.opendata.arcgis.com/datasets/3f8354667d5b4b1b8ad7a6e00c3cf3b1_1
_FUDS_FILE = os.path.join(os.path.dirname(__file__), "fuds_florida.json")
_RCRA_CA_FILE = os.path.join(os.path.dirname(__file__), "rcra_ca_florida.json")
try:
    with open(_FUDS_FILE) as _f:
        _FUDS_DATA = json.load(_f)
    FUDS_SITES = _FUDS_DATA["sites"]
    FUDS_FY    = _FUDS_DATA["fiscal_year"]
except Exception as _e:
    FUDS_SITES = []
    FUDS_FY    = "unknown"
    print(f"WARNING: Could not load FUDS data: {_e}")

try:
    with open(_RCRA_CA_FILE) as _f:
        _rcra_ca_raw = json.load(_f)
    RCRA_CA_DATA         = _rcra_ca_raw.get("facilities", _rcra_ca_raw if isinstance(_rcra_ca_raw, list) else [])
    RCRA_CA_DOWNLOAD_DATE = _rcra_ca_raw.get("download_date", "unknown")
    print(f"Loaded {len(RCRA_CA_DATA)} FL RCRA CA facilities (downloaded {RCRA_CA_DOWNLOAD_DATE})")
except Exception as _e:
    RCRA_CA_DATA = []
    RCRA_CA_DOWNLOAD_DATE = "unknown"
    print(f"Warning: could not load rcra_ca_florida.json: {_e}")

app = Flask(__name__, static_folder='.', static_url_path='')

# ── RCRA CA cache ─────────────────────────────────────────────────────────────
# Caches ECHO RCRA CA results by 0.5-degree grid cell to avoid rate limiting.
# Cache expires after 24 hours. Grid cell = round(lat,1), round(lon,1)
import threading
_rcra_cache = {}
_rcra_cache_lock = threading.Lock()
RCRA_CACHE_TTL = 86400  # 24 hours

def _cache_key(lat, lon):
    """Round to 0.5 degree grid — roughly 30 mile cells."""
    return (round(lat * 2) / 2, round(lon * 2) / 2)

def get_cached_rcra(lat, lon):
    key = _cache_key(lat, lon)
    with _rcra_cache_lock:
        if key in _rcra_cache:
            cached_time, cached_result = _rcra_cache[key]
            if time.time() - cached_time < RCRA_CACHE_TTL:
                return cached_result
    return None

def set_cached_rcra(lat, lon, result):
    key = _cache_key(lat, lon)
    with _rcra_cache_lock:
        _rcra_cache[key] = (time.time(), result)
CORS(app)

@app.route('/')
def index():
    return app.send_static_file('index.html')

# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def bbox(lat, lon, radius_miles):
    """Return (min_lat, max_lat, min_lon, max_lon) for a radius around a point."""
    dlat = radius_miles / 69.0
    dlon = radius_miles / (69.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon

def fdep_query(url, lat, lon, radius_miles, where="1=1", out_fields="*"):
    """FDEP ArcGIS layers — use point+distance+units (projected CRS, accepts WGS84 input)."""
    params = {
        "geometry":       f"{lon},{lat}",
        "geometryType":   "esriGeometryPoint",
        "spatialRel":     "esriSpatialRelIntersects",
        "distance":       radius_miles * 1609.34,
        "units":          "esriSRUnit_Meter",
        "inSR":           "4326",
        "outSR":          "4326",
        "where":          where,
        "outFields":      out_fields,
        "returnGeometry": "true",
        "f":              "json",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "features": []}

def frs_query(url, where, out_fields):
    """EPA FRS ArcGIS layers — plain WHERE clause, no spatial params."""
    try:
        r = requests.get(url, params={
            "where": where, "outFields": out_fields,
            "returnGeometry": "false", "f": "json"
        }, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "features": []}

def eric_query(lat, lon, radius_miles, program_filter=None):
    """Query ERIC layer 8 — the unified FDEP cleanup database with all 11 programs."""
    where = "1=1"
    if program_filter:
        progs = "','".join(program_filter)
        where = f"PROGRAM IN ('{progs}')"
    data = fdep_query(ERIC_LAYER, lat, lon, radius_miles,
        where=where,
        out_fields="SITE_NAME,PROGRAM,SITE_STATUS,ERIC_ID")
    sites = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        name  = str(attrs.get("SITE_NAME") or "Unknown")
        dist  = 999.0
        if geom and "x" in geom and "y" in geom:
            try: dist = round(haversine(lat, lon, float(geom["y"]), float(geom["x"])), 2)
            except: pass
        status = str(attrs.get("SITE_STATUS","") or "")
        nc = status.upper() in ERIC_NC
        eric_id = str(attrs.get("ERIC_ID","") or "")
        site = {"name": name, "distance": dist, "status": status, "nc": nc}
        if eric_id:
            site["nexus_url"] = f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents/{eric_id}/facility!search"
        sites.append(site)
    sites.sort(key=lambda s: s["distance"])
    return sites

def merge_dedup(list1, list2):
    """Merge two site lists, deduplicate by name, keep closest distance."""
    seen = set(); out = []
    for s in sorted(list1 + list2, key=lambda x: x["distance"]):
        if s["name"] not in seen:
            seen.add(s["name"]); out.append(s)
    return out

def frs_spatial(url, lat, lon, radius_miles, out_fields="*"):
    """EPA FRS ArcGIS layers — envelope spatial query with correct xmin,ymin,xmax,ymax order."""
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, radius_miles)
    # ArcGIS envelope: xmin,ymin,xmax,ymax (lon_min,lat_min,lon_max,lat_max)
    envelope = f"{mn_lon},{mn_lat},{mx_lon},{mx_lat}"
    try:
        r = requests.get(url, params={
            "geometry":       envelope,
            "geometryType":   "esriGeometryEnvelope",
            "spatialRel":     "esriSpatialRelIntersects",
            "inSR":           "4269",
            "outSR":          "4269",
            "outFields":      out_fields,
            "returnGeometry": "false",
            "f":              "json",
        }, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "features": []}

def parse_fdep(data, lat, lon, name_field, status_field=None, nc_statuses=None):
    sites = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        name  = str(attrs.get(name_field) or "Unknown")
        dist  = 999.0
        if geom and "x" in geom and "y" in geom:
            try: dist = round(haversine(lat, lon, float(geom["y"]), float(geom["x"])), 2)
            except: pass
        status = str(attrs.get(status_field, "") or "") if status_field else ""
        nc = bool(nc_statuses and status in nc_statuses)
        # Extract Nexus URL from DOCUMENTS field if available
        nexus_url = str(attrs.get("DOCUMENTS", "") or "")
        site = {"name": name, "distance": dist, "status": status, "nc": nc}
        if nexus_url:
            site["nexus_url"] = nexus_url
        sites.append(site)
    sites.sort(key=lambda s: s["distance"])
    return sites

def parse_frs(data, lat, lon, name_field, lat_field, lon_field, radius_miles,
              status_field=None, nc_statuses=None, exclude_statuses=None):
    sites = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        status = str(attrs.get(status_field, "") or "") if status_field else ""
        if exclude_statuses and status in exclude_statuses:
            continue
        flat = float(attrs.get(lat_field, 0) or 0)
        flon = float(attrs.get(lon_field, 0) or 0)
        if not flat or not flon:
            continue
        dist = haversine(lat, lon, flat, flon)
        if dist > radius_miles:
            continue
        nc = bool(nc_statuses and status in nc_statuses)
        sites.append({"name": str(attrs.get(name_field) or "Unknown"),
                      "distance": round(dist, 2), "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return sites

# ── FDEP URLs ─────────────────────────────────────────────────────────────────
DEP_CLEANUP  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/0/query"
FDEP_BROWN   = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/BROWNFIELD_AREAS/MapServer/1/query"
FDEP_BROWN_AREAS = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/BROWNFIELD_AREAS/MapServer/0/query"
ERIC_LAYER   = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/8/query"
FL_SUPERFUND = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/1/query"
CHAZ         = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CHAZ/MapServer/5/query"
STCM_TANKS   = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/1/query"
STCM_LUST    = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/2/query"
SOLID_WASTE  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_WASTE_ICR_BACKG/MapServer/1/query"
ICR          = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_WASTE_ICR_BACKG/MapServer/12/query"

# ── EPA FRS URLs ──────────────────────────────────────────────────────────────
FRS_SEMS      = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21/query"
FRS_SEMS_NPL  = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/22/query"
FRS_ACRES     = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/0/query"
FRS_RCRA      = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/15/query"
FRS_RCRA_ACT  = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/16/query"
FRS_RCRA_LQG  = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/18/query"
FRS_RCRA_TSD  = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/20/query"
# CIMC RCRA Corrective Action polygon layer — same source as EPA's CIMC map
CIMC_RCRA_CA  = "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/NationalRCRABoundaries/FeatureServer/1/query"
# ECHO GeoServer — RCRA facilities with full compliance data, no rate limiting
ECHOGEO_RCRA  = "https://echogeo.epa.gov/arcgis/rest/services/ECHO/Facilities/MapServer/3/query"

# ── DEP Cleanup field constants ───────────────────────────────────────────────
DEP_FIELDS  = "BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY,SOURCE_DATABASE_NAME,SOURCE_DATABASE_ID,DOCUMENTS"
SUPER_WHERE = "CLCC_CLEANUP_CATEGORY_KEY='SUPER'"
CONT_WHERE  = "CLCC_CLEANUP_CATEGORY_KEY IN ('OTHCU','PFAS')"
LUST_WHERE  = "CLCC_CLEANUP_CATEGORY_KEY='PETRO'"
BROWN_WHERE = "CLCC_CLEANUP_CATEGORY_KEY='BROWN'"
VOL_WHERE   = "SOURCE_DATABASE_NAME IN ('DRYCLEANING','RESPONSPARTY')"
DEP_NC = {"SRCO","ISSA","SSA","PA","SI","RI","FS","RD","RA","OAM",
           "OPEN","ACTIVE","INPROCESS","AWAITFUND","AWAITSITEACCESS","ELIGREVIEW","ONHOLD"}
# ERIC layer 8 SITE_STATUS values that are non-compliant (active cleanup)
ERIC_NC = {"OPEN","ONHOLD","INPROCESS"}  # CLOSED and CLOSEDWCOND = complete; ONHOLD = paused but active

# ── EPA ECHO RCRA ─────────────────────────────────────────────────────────────
def echo_rcra(lat, lon, radius_miles, handler_types):
    """Query EPA ECHO RCRA facilities. Tries multiple endpoints to avoid rate limiting."""
    endpoints = [
        "https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info",
        "https://echo.epa.gov/echo/rcra_rest_services.get_facility_info",
    ]
    params = {
        "output":      "JSON",
        "p_lat":       lat,
        "p_lon":       lon,
        "p_radius_mi": radius_miles,
        "p_htype":     handler_types,
        "qcolumns":    "1,2,3,4,5,6,38,39,40",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://echo.epa.gov/",
    }
    last_error = "Unknown error"
    for url in endpoints:
        for attempt in range(2):
            try:
                if attempt > 0:
                    time.sleep(3)
                r = requests.get(url, params=params, headers=headers, timeout=15)
                if r.status_code == 429:
                    last_error = "Rate limited (429)"
                    time.sleep(5)
                    continue
                r.raise_for_status()
                facilities = r.json().get("Results", {}).get("Facilities", [])
                sites = []
                for f in facilities:
                    flat = float(f.get("FacLat", 0) or 0)
                    flon = float(f.get("FacLong", 0) or 0)
                    dist = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
                    status = f.get("RCRAComplianceStatus", "") or ""
                    nc = "CA" in handler_types and status not in ["No Violation Identified", ""]
                    sites.append({"name": f.get("FacName","Unknown"), "distance": dist, "status": status, "nc": nc})
                sites.sort(key=lambda s: s["distance"])
                return {"count": len(sites), "sites": sites}
            except Exception as e:
                last_error = str(e)
    return {"count": 0, "sites": [], "error": f"Failed: {last_error}"}


def cimc_rcra_ca(lat, lon, radius_miles):
    """Delegates to echogeo_rcra_all."""
    return echogeo_rcra_all(lat, lon, radius_miles)


def static_rcra_ca(lat, lon, radius_miles):
    """
    Query FL RCRA CA facilities from static JSON file (ECHO RCRA_FACILITIES.csv, FL only).
    Source: EPA ECHO RCRAInfo download, ACTIVE_SITE field containing 'A' (Corrective Action).
    Updated: from RCRA_FACILITIES.csv download date.
    No rate limiting, no network dependency.
    """
    sites = []
    for facility in RCRA_CA_DATA:
        flat = facility.get("lat", 0)
        flon = facility.get("lon", 0)
        if not flat or not flon:
            continue
        dist = round(haversine(lat, lon, flat, flon), 2)
        if dist > radius_miles:
            continue
        sites.append({
            "name": facility.get("name", "Unknown"),
            "distance": dist,
            "status": "Corrective Action",
            "nc": True  # CA = nc by definition
        })
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}


def echogeo_rcra_all(lat, lon, radius_miles):
    """
    Query RCRA facilities from ECHO GeoServer — no rate limiting.
    CA identified by RCR_STATUS containing 'A' flag (Corrective Action workload).
    TSD identified by RCRA_UNIVERSE containing TSD.
    Generators identified by RCRA_UNIVERSE (LQG/SQG/VSQG/CESQG).
    Results cached for 24 hours by 0.5-degree grid cell.
    """
    cached = get_cached_rcra(lat, lon)
    if cached is not None:
        return cached
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, radius_miles)
    envelope = f"{mn_lon},{mn_lat},{mx_lon},{mx_lat}"
    fields = ("RCR_NAME,RCR_CITY,RCR_STATE,FAC_LAT,FAC_LONG,"
              "RCRA_UNIVERSE,RCRA_CURR_COMPL_STATUS,RCRA_CURR_SNC,RCR_STATUS,RCRA_IDS")
    try:
        r = requests.get(ECHOGEO_RCRA, params={
            "geometry": envelope, "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "where": "RCR_STATE='FL'",
            "outFields": fields, "returnGeometry": "false", "f": "json",
        }, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        empty = {"count": 0, "sites": [], "error": str(e)}
        return {"ca": empty, "tsd": dict(empty), "gen": dict(empty)}
    if "error" in data:
        empty = {"count": 0, "sites": [], "error": data["error"]}
        return {"ca": empty, "tsd": dict(empty), "gen": dict(empty)}
    ca_sites, tsd_sites, gen_sites = [], [], []
    for feat in data.get("features", []):
        attrs    = feat.get("attributes", {})
        flat     = float(attrs.get("FAC_LAT", 0) or 0)
        flon     = float(attrs.get("FAC_LONG", 0) or 0)
        if not flat or not flon: continue
        dist     = round(haversine(lat, lon, flat, flon), 2)
        name     = str(attrs.get("RCR_NAME","Unknown") or "Unknown")
        universe = str(attrs.get("RCRA_UNIVERSE","") or "").upper()
        status   = str(attrs.get("RCRA_CURR_COMPL_STATUS","") or "")
        snc      = str(attrs.get("RCRA_CURR_SNC","") or "")
        rcr_status = str(attrs.get("RCR_STATUS","") or "")
        site = {"name": name, "distance": dist, "status": status, "nc": False}

        # Parse flags from RCR_STATUS — format: "Active (FLAGS)"
        # e.g. "Active (H    )" "Active ( PA  )" "Active (A    )" "Active (HA   )"
        import re as _re
        flag_match = _re.search(r'\(([^)]+)\)', rcr_status)
        flag_str = flag_match.group(1).strip() if flag_match else ""
        flags = set(flag_str.split())  # e.g. {"PA"} or {"H"} or {"A"} or {"HA"}

        # CA — 'A' appears as standalone flag or part of multi-flag like HA
        is_ca = any(f == "A" or f.endswith("A") for f in flags) or "A" in flag_str
        if is_ca and dist <= radius_miles:
            nc = status not in ["No Violation Identified",""] or snc == "Yes"
            ca_site = dict(site)
            ca_site["nc"] = nc
            ca_sites.append(ca_site)

        # TSD — 'P' flag = active RCRA permit = TSD facility
        # e.g. "Active ( PA  )" where PA = Permit + something
        is_tsd = (any("P" in f for f in flags) or
                  any(t in universe for t in ["TSD","TSDF","LEGACY TSDF"]))
        if is_tsd and dist <= 0.5:
            tsd_site = dict(site)
            tsd_site["nc"] = status not in ["No Violation Identified", ""]
            tsd_sites.append(tsd_site)

        # Generators — within 0.05 miles
        if any(t in universe for t in ["LQG","SQG","VSQG","CESQG"]) and dist <= 0.05:
            gen_sites.append(dict(site))

    result = {
        "ca":  {"count": len(ca_sites),  "sites": sorted(ca_sites,  key=lambda s: s["distance"])},
        "tsd": {"count": len(tsd_sites), "sites": sorted(tsd_sites, key=lambda s: s["distance"])},
        "gen": {"count": len(gen_sites), "sites": sorted(gen_sites, key=lambda s: s["distance"])},
    }
    set_cached_rcra(lat, lon, result)
    return result


def frs_rcra_all(lat, lon):
    """
    Query RCRA TSD and LQG facilities from EPA FRS — no rate limiting.
    CA (corrective action) requires ECHO compliance data — handled separately.
    TSD from layer 20, LQG generators from layer 18.
    """
    fields = "PRIMARY_NAME,ACTIVE_STATUS,LATITUDE83,LONGITUDE83"

    # TSD facilities — layer 20, within 0.5 miles
    tsd_data = frs_spatial(FRS_RCRA_TSD, lat, lon, 0.5, out_fields=fields)
    tsd_sites = []
    for feat in tsd_data.get("features", []):
        attrs = feat.get("attributes", {})
        flat = float(attrs.get("LATITUDE83", 0) or 0)
        flon = float(attrs.get("LONGITUDE83", 0) or 0)
        if not flat or not flon: continue
        dist = haversine(lat, lon, flat, flon)
        if dist > 0.5: continue
        status = str(attrs.get("ACTIVE_STATUS","") or "")
        tsd_sites.append({"name": str(attrs.get("PRIMARY_NAME","Unknown")),
                          "distance": round(dist,2), "status": status, "nc": False})

    # LQG generators — layer 18, within 0.05 miles
    gen_data = frs_spatial(FRS_RCRA_LQG, lat, lon, 0.05, out_fields=fields)
    gen_sites = []
    for feat in gen_data.get("features", []):
        attrs = feat.get("attributes", {})
        flat = float(attrs.get("LATITUDE83", 0) or 0)
        flon = float(attrs.get("LONGITUDE83", 0) or 0)
        if not flat or not flon: continue
        dist = haversine(lat, lon, flat, flon)
        if dist > 0.05: continue
        status = str(attrs.get("ACTIVE_STATUS","") or "")
        gen_sites.append({"name": str(attrs.get("PRIMARY_NAME","Unknown")),
                          "distance": round(dist,2), "status": status, "nc": False})

    # CA requires ECHO — return empty, will be filled by ECHO fallback
    return {
        "ca":  {"count": 0, "sites": [], "note": "CA requires ECHO compliance data"},
        "tsd": {"count": len(tsd_sites), "sites": sorted(tsd_sites, key=lambda s: s["distance"])},
        "gen": {"count": len(gen_sites), "sites": sorted(gen_sites, key=lambda s: s["distance"])},
    }


def echo_rcra_all(lat, lon):
    """
    Query all RCRA facility types in a single ECHO call to avoid rate limiting.
    Returns dict keyed by category: ca, tsd, gen.
    """
    url = "https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info"
    # Query all types at once using the widest radius — filter by type afterward
    params = {
        "output":      "JSON",
        "p_lat":       lat,
        "p_lon":       lon,
        "p_radius_mi": 1.0,  # widest radius — we filter per category below
        "qcolumns":    "1,2,3,4,5,6,38,39,40,41",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Phase I ESA Research Tool)",
        "Accept": "application/json",
    }
    last_error = "Unknown error"
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(5 * attempt)  # 5s, 10s backoff
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 429:
                last_error = "Rate limited (429)"
                time.sleep(3)
                continue
            r.raise_for_status()
            facilities = r.json().get("Results", {}).get("Facilities", [])
            ca_sites, tsd_sites, gen_sites = [], [], []
            for f in facilities:
                flat = float(f.get("FacLat", 0) or 0)
                flon = float(f.get("FacLong", 0) or 0)
                dist = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
                status = f.get("RCRAComplianceStatus", "") or ""
                htype = str(f.get("RCRAHandlerType", "") or "").upper()
                name  = f.get("FacName","Unknown")
                # CA — within 1 mile
                if dist <= 1.0 and "CA" in htype:
                    nc = status not in ["No Violation Identified",""]
                    ca_sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
                # TSD — within 0.5 miles
                if dist <= 0.5 and any(t in htype for t in ["TSD","TSDF","LQTSDF"]):
                    tsd_sites.append({"name": name, "distance": dist, "status": status, "nc": False})
                # Generators — within 0.05 miles
                if dist <= 0.05 and any(t in htype for t in ["LQG","SQG","VSQG","CESQG"]):
                    gen_sites.append({"name": name, "distance": dist, "status": status, "nc": False})
            return {
                "ca":  {"count": len(ca_sites),  "sites": sorted(ca_sites,  key=lambda s: s["distance"])},
                "tsd": {"count": len(tsd_sites), "sites": sorted(tsd_sites, key=lambda s: s["distance"])},
                "gen": {"count": len(gen_sites), "sites": sorted(gen_sites, key=lambda s: s["distance"])},
            }
        except Exception as e:
            last_error = str(e)
    empty = {"count": 0, "sites": [], "error": f"Failed: {last_error}"}
    return {"ca": empty, "tsd": empty, "gen": empty}

# ── USACE FUDS ────────────────────────────────────────────────────────────────
def fuds(lat, lon, radius_miles):
    """
    Query USACE FUDS from static FY2024 Florida data.
    USACE ArcGIS servers block cloud hosting IPs — static file is more reliable.
    Update fuds_florida.json each October when USACE releases new FY data.
    Source: https://geospatial-usace.opendata.arcgis.com/datasets/3f8354667d5b4b1b8ad7a6e00c3cf3b1_1
    """
    if not FUDS_SITES:
        return {"count": 0, "sites": [], "error": "FUDS data file not loaded"}
    sites = []
    for record in FUDS_SITES:
        flat = record.get("lat", 0)
        flon = record.get("lon", 0)
        if not flat or not flon:
            continue
        dist = haversine(lat, lon, flat, flon)
        if dist > radius_miles:
            continue
        status = record.get("status", "")
        # NC if property has active projects (not all closed/no projects)
        nc = status not in {
            "Properties with all projects at site closeout",
            "Properties without projects"
        }
        sites.append({
            "name":     record["name"],
            "distance": round(dist, 2),
            "status":   status,
            "nc":       nc
        })
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites, "data_source": f"USACE FUDS {FUDS_FY} (static)"}

# ── FRS NPL ───────────────────────────────────────────────────────────────────
def frs_npl(lat, lon, radius_miles, status_filter=None):
    # INTEREST_TYPE values: "SUPERFUND NPL", "SUPERFUND (NON-NPL)"
    # ACTIVE_STATUS values: "NOT ON THE NPL", "DELETED FROM THE FINAL NPL", "CURRENTLY ON THE FINAL NPL" etc
    # Use SEMS_NPL layer (22) — contains only NPL sites, no filtering needed
    data = frs_spatial(FRS_SEMS_NPL, lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,ACTIVE_STATUS,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        active   = str(attrs.get("ACTIVE_STATUS","") or "")
        interest = "SUPERFUND NPL"  # layer 22 is NPL only
        flat = float(attrs.get("LATITUDE83", 0) or 0)
        flon = float(attrs.get("LONGITUDE83", 0) or 0)
        if not flat or not flon:
            continue
        dist = haversine(lat, lon, flat, flon)
        if dist > radius_miles:
            continue
        status = active
        nc = active.upper() not in {"DELETED FROM THE FINAL NPL"}
        # Apply status filter (for delisted query)
        if status_filter and active not in status_filter:
            continue
        sites.append({"name": str(attrs.get("PRIMARY_NAME","Unknown")),
                      "distance": round(dist,2), "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── CERCLA ────────────────────────────────────────────────────────────────────
def cercla(lat, lon, radius_miles):
    # CERCLA non-NPL sites: INTEREST_TYPE = "SUPERFUND (NON-NPL)"
    # SEMS layer 21 contains all SEMS sites — filter to non-NPL only
    data = frs_spatial(FRS_SEMS, lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,INTEREST_TYPE,ACTIVE_STATUS,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = []
    for feat in data.get("features", []):
        attrs    = feat.get("attributes", {})
        interest = str(attrs.get("INTEREST_TYPE","") or "")
        active   = str(attrs.get("ACTIVE_STATUS","") or "")
        # Only non-NPL CERCLA/SEMS sites (NPL sites are in layer 22 / frs_npl)
        if interest not in {"SUPERFUND (NON-NPL)","SUPERFUND NON-NPL","CERCLA","NOT ON THE NPL"}:
            continue
        flat = float(attrs.get("LATITUDE83", 0) or 0)
        flon = float(attrs.get("LONGITUDE83", 0) or 0)
        if not flat or not flon:
            continue
        dist = haversine(lat, lon, flat, flon)
        if dist > radius_miles:
            continue
        # CERCLA non-NPL: "NOT ON THE NPL" means assessed, no further action
        # Only flag NC if there is an active removal action underway
        nc = active.upper() in {"REMOVAL ACTION UNDERWAY", "REMOVAL ACTION COMPLETE - REMEDIAL ACTION UNDERWAY"}
        sites.append({"name": str(attrs.get("PRIMARY_NAME","Unknown")),
                      "distance": round(dist,2), "status": active, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── ERNS ──────────────────────────────────────────────────────────────────────
def erns(zipcode):
    if not zipcode:
        return {"count": 0, "sites": [], "note": "ZIP not provided"}
    try:
        r = requests.get(f"https://data.epa.gov/efservice/ERNS_INCIDENTS/ZIP_CODE/{zipcode}/rows/0:100/JSON", timeout=20)
        r.raise_for_status()
        sites = [{"name": rec.get("FACILITY_NAME") or rec.get("COMPANY_NAME") or "ERNS Incident",
                  "distance": 0.0, "status": rec.get("INCIDENT_TYPE_DESCRIPTION","") or "", "nc": True}
                 for rec in r.json()]
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── Main query endpoint ───────────────────────────────────────────────────────
@app.route("/query", methods=["GET"])
def query():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon are required"}), 400
    if not (24.0 <= lat <= 31.5):
        return jsonify({"error": f"Latitude {lat} outside Florida bounds (24-31.5)"}), 400
    if not (-87.5 <= lon <= -79.5):
        return jsonify({"error": f"Longitude {lon} outside Florida bounds (-87.5 to -79.5). Make sure it is negative."}), 400
    zipcode = request.args.get("zip", "")
    res = {}

    def get_state_superfund():
        s1 = parse_fdep(fdep_query(FL_SUPERFUND, lat, lon, 1.0,
            out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY"),
            lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
        s0 = parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 1.0, where=SUPER_WHERE, out_fields=DEP_FIELDS),
            lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
        seen = set(); out = []
        for s in sorted(s1+s0, key=lambda x: x["distance"]):
            if s["name"] not in seen: seen.add(s["name"]); out.append(s)
        return {"count": len(out), "sites": out}

    def get_delisted():
        r = frs_npl(lat, lon, 0.5, status_filter=["DELETED FROM THE FINAL NPL"])
        for s in r.get("sites", []): s["nc"] = False
        return r

    def get_haz():
        data = fdep_query(CHAZ, lat, lon, 0.5, out_fields="ME_NAME,FAC_INS_TYPE,GENERATOR,PERMITTED_CONSENTED")
        raw = []
        for feat in data.get("features", []):
            attrs = feat.get("attributes", {}); geom = feat.get("geometry", {})
            name = str(attrs.get("ME_NAME") or "Unknown"); dist = 999.0
            if geom and "x" in geom and "y" in geom:
                try: dist = round(haversine(lat, lon, float(geom["y"]), float(geom["x"])), 2)
                except: pass
            gen = str(attrs.get("GENERATOR","") or ""); perm = str(attrs.get("PERMITTED_CONSENTED","") or "")
            # CHAZ is a facility registry only — being a generator is not a violation.
            # NC is determined by ECHO RCRA compliance data, not generator status.
            # All CHAZ facilities count toward paragraph 2 total but never generate bullets.
            nc = False
            raw.append({"name": name, "distance": dist, "status": str(attrs.get("FAC_INS_TYPE","") or ""), "nc": nc})
        # Deduplicate by name — keep closest, upgrade to NC if any record is NC
        seen = {}
        for s in sorted(raw, key=lambda x: x["distance"]):
            key = s["name"].strip().upper()
            if key not in seen:
                seen[key] = s
            else:
                if s["nc"]:
                    seen[key]["nc"] = True
        sites = sorted(seen.values(), key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}

    def get_lust():
        # DEP Cleanup layer 0 — PETRO category
        s1 = parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=LUST_WHERE, out_fields=DEP_FIELDS),
            lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
        # STCM PCTS discharge layer
        s2 = parse_fdep(fdep_query(STCM_LUST, lat, lon, 0.5, out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE"),
            lat, lon, "SITE_NAME", "SITE_STATUS", {"OPEN","ACTIVE","Active","Open"})
        # ERIC layer 8 — Petroleum Restoration Program and Responsible Party petroleum sites
        # ERIC layer 8 — petroleum restoration program only (Responsible Party goes to voluntary)
        s3 = eric_query(lat, lon, 0.5, program_filter=["Petroleum Restoration Program"])
        all_sites = merge_dedup(s1 + s2, s3)
        return {"count": len(all_sites), "sites": all_sites}

    def get_brownfields():
        fdep_raw_brown = fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS)
        fdep_sites = parse_fdep(fdep_raw_brown, lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)

        # Build initial coord list from DEP Cleanup geometry
        bf_coords = []
        bf_names  = []
        for f in fdep_raw_brown.get("features", []):
            geom = f.get("geometry", {})
            name = str(f.get("attributes", {}).get("BUSINESS_NAME","") or "")
            if geom and "x" in geom and "y" in geom:
                bf_coords.append((float(geom["y"]), float(geom["x"])))
                bf_names.append(name)

        # FDEP Brownfields Areas layer — official FDEP brownfields registry
        # Spatial ref is FL State Plane (102967) — use attribute lat/lon filter instead
        mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, 0.5)
        try:
            r = requests.get(FDEP_BROWN, params={
                "where": (f"LATITUDE >= {mn_lat} AND LATITUDE <= {mx_lat} "
                          f"AND LONGITUDE >= {mn_lon} AND LONGITUDE <= {mx_lon}"),
                "outFields": "AREA_NAME,SITE_NAME,SITE_ID,REMEDIATION,LATITUDE,LONGITUDE",
                "returnGeometry": "false",
                "f": "json"
            }, timeout=15)
            fdep_bf_data = r.json()
        except:
            fdep_bf_data = {}
        # Parse brownfield site records using attribute lat/lon
        # Dedup by SITE_ID and coordinate proximity (0.01 miles ~ 50 feet)
        seen_bf = set()       # SITE_IDs already added

        def coord_is_dup(slat, slon, sname, threshold=0.15):
            """Return True if within threshold miles AND shares a significant word stem
            with any existing brownfield site. Truncates to 5 chars for typo tolerance."""
            import re
            stopwords = {"THE","OF","A","AN","AND","AT","IN","INC","LLC","CORP",
                         "SITE","SITES","AREA","BROWNFIELD","BROWNFIELDS","COUNTY",
                         "PARK","FORMER","FLORIDA","LANDFILL","HISTORIC","WASTE",
                         "CLEANUP","INDUSTRIAL","COMMERCIAL","PROPERTY","STORMWATER",
                         "POND","DETENTION","CLASS","III","II","I"}
            def sig(name):
                words = re.sub(r'[^A-Z0-9 ]', '', name.upper()).split()
                return {w[:5] for w in words if w not in stopwords and len(w) >= 3}
            name_sig = sig(sname)
            for c, cname in zip(bf_coords, bf_names):
                if haversine(slat, slon, c[0], c[1]) < threshold:
                    if name_sig & sig(cname):  # any word stem overlap
                        return True
            return False

        # First add DEP Cleanup layer 0 sites (already in fdep_sites) — no _lat/_lon stored, skip
        # bf_coords already populated from fdep_raw_brown geometry above

        for feat in fdep_bf_data.get("features", []):
            attrs = feat.get("attributes", {})
            name    = str(attrs.get("SITE_NAME","") or attrs.get("AREA_NAME","Unknown BF Site") or "Unknown BF Site")
            site_id = str(attrs.get("SITE_ID","") or "")
            status  = str(attrs.get("REMEDIATION","") or "")
            flat    = float(attrs.get("LATITUDE", 0) or 0)
            flon    = float(attrs.get("LONGITUDE", 0) or 0)
            if not flat or not flon: continue
            if site_id in seen_bf: continue
            if site_id: seen_bf.add(site_id)
            dist = round(haversine(lat, lon, flat, flon), 2)
            if dist > 0.5: continue
            if coord_is_dup(flat, flon, name): continue
            nc = status.upper() in DEP_NC or status.upper() in {"OPEN","ACTIVE","INPROCESS","SRCO"}
            bf_coords.append((flat, flon))
            bf_names.append(name)
            fdep_sites.append({"name": name, "distance": dist, "status": status, "nc": nc})

        # Also query layer 0 — Brownfield Areas (designated area polygons)
        # Catches sites like Dansville North that have area designation but no layer 1 BSRA yet
        try:
            r0 = requests.get(FDEP_BROWN_AREAS, params={
                "where": (f"LATITUDE >= {mn_lat} AND LATITUDE <= {mx_lat} "
                          f"AND LONGITUDE >= {mn_lon} AND LONGITUDE <= {mx_lon}"),
                "outFields": "AREA_NAME,AREA_ID,LATITUDE,LONGITUDE,RESOLUTION_DATE",
                "returnGeometry": "false",
                "f": "json"
            }, timeout=15)
            fdep_bf_area_data = r0.json()
        except:
            fdep_bf_area_data = {}
        # Parse area records — only add if not already represented by coordinate proximity + name
        for feat in fdep_bf_area_data.get("features", []):
            attrs = feat.get("attributes", {})
            area_id = str(attrs.get("AREA_ID","") or "")
            name    = str(attrs.get("AREA_NAME","Unknown BF Area") or "Unknown BF Area")
            flat    = float(attrs.get("LATITUDE", 0) or 0)
            flon    = float(attrs.get("LONGITUDE", 0) or 0)
            if not flat or not flon: continue
            dist = round(haversine(lat, lon, flat, flon), 2)
            if dist > 0.5: continue
            # Skip if area already represented by a layer 1 site (same area_id prefix)
            area_prefix = area_id[:10]
            if any(s_id[:10] == area_prefix for s_id in seen_bf):
                continue
            if coord_is_dup(flat, flon, name): continue
            bf_coords.append((flat, flon))
            bf_names.append(name)
            fdep_sites.append({"name": name, "distance": dist,
                               "status": "Brownfield Area", "nc": False})

        epa_data = frs_spatial(FRS_ACRES, lat, lon, 0.5,
            out_fields="PRIMARY_NAME,LATITUDE83,LONGITUDE83,ACTIVE_STATUS,INTEREST_TYPE,REGISTRY_ID")
        # Only include confirmed brownfield properties from ACRES
        # Deduplicate EPA ACRES by registry ID first (EPA sometimes returns same site twice)
        seen_registry = set()
        epa_sites = []
        for feat in epa_data.get("features", []):
            attrs = feat.get("attributes", {})
            interest = str(attrs.get("INTEREST_TYPE","") or "")
            if interest != "BROWNFIELDS PROPERTY":
                continue
            registry_id = str(attrs.get("REGISTRY_ID","") or "")
            if registry_id and registry_id in seen_registry:
                continue
            if registry_id:
                seen_registry.add(registry_id)
            flat = float(attrs.get("LATITUDE83", 0) or 0)
            flon = float(attrs.get("LONGITUDE83", 0) or 0)
            if not flat or not flon:
                continue
            dist = haversine(lat, lon, flat, flon)
            if dist > 0.5:
                continue
            st = str(attrs.get("ACTIVE_STATUS","") or "").upper()
            nc = st in {"CLEANUP UNDERWAY","CLEANUP ONGOING","ACTIVE","OPEN"}
            epa_sites.append({"name": str(attrs.get("PRIMARY_NAME","Unknown")),
                              "distance": round(dist,2), "status": str(attrs.get("ACTIVE_STATUS","") or ""),
                              "nc": nc, "_lat": flat, "_lon": flon})
        # Drop EPA ACRES sites that are coordinate+name duplicates of existing brownfield sites
        filtered_epa = []
        for epa in epa_sites:
            if not coord_is_dup(epa["_lat"], epa["_lon"], epa["name"]):
                bf_coords.append((epa["_lat"], epa["_lon"]))
                bf_names.append(epa["name"])
                filtered_epa.append(epa)
        # Remove internal coords before returning
        for s in filtered_epa:
            s.pop("_lat", None); s.pop("_lon", None)
        seen = set(); out = []
        for s in sorted(fdep_sites + filtered_epa, key=lambda x: x["distance"]):
            if s["name"] not in seen: seen.add(s["name"]); out.append(s)
        return {"count": len(out), "sites": out}

    def mk(fn): return lambda p: {"count": len(p), "sites": p}

    # RCRA: ECHO GeoServer for all types — no rate limiting, full compliance data
    echogeo_results = echogeo_rcra_all(lat, lon, 1.0)
    # FRS as fallback for TSD/gen if GeoServer fails
    if echogeo_results["tsd"].get("error") or echogeo_results["gen"].get("error"):
        frs_rcra_results = frs_rcra_all(lat, lon)
        echogeo_results["tsd"] = frs_rcra_results["tsd"]
        echogeo_results["gen"] = frs_rcra_results["gen"]

    def get_solid():
        data = fdep_query(SOLID_WASTE, lat, lon, 0.5,
            where="FACILITY_STATUS NOT IN ('Closed','CLOSED','Closed, No Gw Monitoring')",
            out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE")
        sites = parse_fdep(data, lat, lon, "FACILITY_NAME", "FACILITY_STATUS", set())
        # Deduplicate by name
        seen = {}
        for s in sorted(sites, key=lambda x: x["distance"]):
            if s["name"] not in seen:
                seen[s["name"]] = s
        return {"count": len(seen), "sites": list(seen.values())}

    task_map = {
        "npl":            lambda: frs_npl(lat, lon, 1.0, status_filter=["Currently on the Final NPL","Proposed for NPL"]),
        "fuds":           lambda: fuds(lat, lon, 1.0),
        "rcra_ca":        lambda: static_rcra_ca(lat, lon, 1.0),
        "state_superfund":get_state_superfund,
        "npl_del":        get_delisted,
        "cercla":         lambda: cercla(lat, lon, 0.5),
        "rcra_tsd":       lambda: echogeo_results["tsd"],
        "haz":            get_haz,
        "cont":           lambda: (lambda s: {"count": len(s), "sites": s})(merge_dedup(
                              parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields=DEP_FIELDS), lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC),
                              eric_query(lat, lon, 0.5, program_filter=["State Funded Cleanup Program","Site Investigation Section","Hazardous Waste Cleanup Program","CERCLA Site Screening Program","State and Tribal Response Program","State-owned Lands Cleanup Program"]))),
        "solid":          lambda: get_solid(),
        "lust":           get_lust,
        "vol":            lambda: (lambda s: {"count": len(s), "sites": s})(merge_dedup(
                              parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=VOL_WHERE, out_fields=DEP_FIELDS), lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC),
                              eric_query(lat, lon, 0.5, program_filter=["Drycleaning Solvent Cleanup Program","Responsible Party Cleanup"]))),
        "brown":          get_brownfields,
        "ust":            lambda: (lambda data: {"count": len(data), "sites": data})(
                              # UST: never NC — registered tanks are compliant by definition
                              # NC only comes from RCRA CA/compliance data, not tank registration status
                              [{"name": s["name"], "distance": s["distance"],
                                "status": s["status"], "nc": False}
                               for s in parse_fdep(
                                   fdep_query(STCM_TANKS, lat, lon, 0.05,
                                       out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_CLEANUP_STATUS"),
                                   lat, lon, "FACILITY_NAME", "FACILITY_STATUS", set())]),
        "rcra_gen":       lambda: echogeo_results["gen"],
        "ic":             lambda: (lambda r: {
            "count": len(r),
            "sites": r
        })(list({attrs["PRIMARY_SITE_ID"] or attrs["PRIMARY_SITE_NAME"]: {
            "name": attrs["PRIMARY_SITE_NAME"] or "Unnamed IC Site",
            "distance": 0.0,
            "status": "ACTIVE" if not attrs.get("END_DATE") else "INACTIVE",
            "nc": not attrs.get("END_DATE")
        } for feat in requests.get(ICR, params={
            "geometry": f"{lon-0.001},{lat-0.001},{lon+0.001},{lat+0.001}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "outFields": "PRIMARY_SITE_NAME,PRIMARY_SITE_ID,BOUNDARY_RESTRICTIONS,END_DATE",
            "returnGeometry": "false", "f": "json"
        }, timeout=15).json().get("features", [])
        for attrs in [feat["attributes"]]
        if attrs.get("PRIMARY_SITE_NAME") or attrs.get("PRIMARY_SITE_ID")
        }.values())),
        "erns":           lambda: erns(zipcode),
    }

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_to_key = {executor.submit(fn): key for key, fn in task_map.items()}
        try:
            for future in as_completed(future_to_key, timeout=60):
                key = future_to_key[future]
                try:
                    res[key] = future.result()
                except Exception as e:
                    res[key] = {"count": 0, "sites": [], "error": str(e)}
        except Exception:
            for future, key in future_to_key.items():
                if key not in res:
                    res[key] = {"count": 0, "sites": [], "error": "Query timed out"}

    # ── Cross-category deduplication ─────────────────────────────────────────
    # A site appearing in multiple DEP Cleanup categories should only be counted
    # in the highest-priority category. Priority: brown > state_superfund > lust > cont > vol
    dedup_priority = ["brown", "state_superfund", "lust", "cont", "vol"]
    dedup_stopwords = {"THE","OF","A","AN","AND","AT","IN","INC","LLC","CORP",
                       "SITE","SITES","AREA","BROWNFIELD","BROWNFIELDS","COUNTY",
                       "PARK","FORMER","FLORIDA","LANDFILL","HISTORIC","WASTE",
                       "CLEANUP","INDUSTRIAL","COMMERCIAL","PROPERTY","PART",
                       "CLASS","III","II","I","ST","RD","AVE","BLVD","DR","LN"}

    def name_sig(name):
        import re
        words = re.sub(r'[^A-Z0-9 ]', '', name.strip().upper()).split()
        words = [w[:5] for w in words if w not in dedup_stopwords and len(w) >= 3]
        return frozenset(words) if words else frozenset({name.strip().upper()[:10]})

    seen_sigs = []
    for category in dedup_priority:
        if category not in res or "sites" not in res[category]:
            continue
        filtered = []
        for site in res[category].get("sites", []):
            sig = name_sig(site["name"])
            is_dup = any(len(sig & seen) >= 2 for seen in seen_sigs)
            if not is_dup:
                seen_sigs.append(sig)
                filtered.append(site)
        res[category]["sites"] = filtered
        res[category]["count"] = len(filtered)

    # Remove vol sites that duplicate brownfield sites
    # Use broader stopwords (keep NORTH/SOUTH/CENTRAL) for this specific check
    if "brown" in res and "vol" in res:
        import re
        bf_stopwords = {"THE","OF","A","AN","AND","AT","IN","INC","LLC","CORP",
                        "SITE","SITES","AREA","BROWNFIELD","BROWNFIELDS","COUNTY",
                        "PARK","FORMER","FLORIDA","WASTE","CLEANUP","INDUSTRIAL",
                        "COMMERCIAL","PROPERTY","PART","CLASS","III","II","I",
                        "ST","RD","AVE","BLVD","DR","LN"}
        def bf_sig(name):
            words = re.sub(r'[^A-Z0-9 ]', '', name.strip().upper()).split()
            words = [w[:5] for w in words if w not in bf_stopwords and len(w) >= 3]
            return frozenset(words) if words else frozenset({name.strip().upper()[:10]})
        brown_sigs = [bf_sig(s["name"]) for s in res["brown"].get("sites", [])]
        vol_filtered = [s for s in res["vol"].get("sites", [])
                        if not any(len(bf_sig(s["name"]) & bs) >= 2 for bs in brown_sigs)]
        res["vol"]["sites"] = vol_filtered
        res["vol"]["count"] = len(vol_filtered)

    return jsonify(res)


# ── Debug endpoint ────────────────────────────────────────────────────────────
@app.route("/debug", methods=["GET"])
def debug():
    db = request.args.get("db", "dep_cont")
    try:
        lat = float(request.args.get("lat", 27.745717))
        lon = float(request.args.get("lon", -82.68471))
    except ValueError:
        return jsonify({"error": "bad lat/lon"}), 400

    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, 0.5)
    where_b = f"LATITUDE83>={mn_lat} AND LATITUDE83<={mx_lat} AND LONGITUDE83>={mn_lon} AND LONGITUDE83<={mx_lon}"

    routes = {
        "chaz":           lambda: fdep_query(CHAZ, lat, lon, 0.5, out_fields="ME_NAME,FAC_INS_TYPE,GENERATOR,PERMITTED_CONSENTED"),
        "stcm_lust":      lambda: fdep_query(STCM_LUST, lat, lon, 0.5, out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE"),
        "stcm_tanks":     lambda: fdep_query(STCM_TANKS, lat, lon, 0.05, out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_CLEANUP_STATUS"),
        "solid":          lambda: fdep_query(SOLID_WASTE, lat, lon, 0.5, out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE"),
        "solid_filtered": lambda: fdep_query(SOLID_WASTE, lat, lon, 0.5,
            where="FACILITY_STATUS NOT IN ('Closed','CLOSED','Closed, No Gw Monitoring','Closed, Gw Monitoring')",
            out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE"),
        "solid_parsed":   lambda: (lambda d: {"raw_count": len(d.get("features",[])), 
            "parsed": parse_fdep(d, lat, lon, "FACILITY_NAME", "FACILITY_STATUS", set())})(
            fdep_query(SOLID_WASTE, lat, lon, 0.5,
            where="FACILITY_STATUS NOT IN ('Closed','CLOSED','Closed, No Gw Monitoring','Closed, Gw Monitoring')",
            out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE")),
        "ic":             lambda: requests.get(ICR, params={
            "geometry": f"{lon-0.001},{lat-0.001},{lon+0.001},{lat+0.001}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "outFields": "PRIMARY_SITE_NAME,PRIMARY_SITE_ID,BOUNDARY_RESTRICTIONS,BOUNDARY_CONTAMINATIONS,END_DATE",
            "returnGeometry": "false", "f": "json"
        }, timeout=15).json(),
        "ic_envelope":    lambda: requests.get(ICR, params={
            "geometry": f"{lon-0.01},{lat-0.01},{lon+0.01},{lat+0.01}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "outFields": "PRIMARY_SITE_NAME,PRIMARY_SITE_ID,BOUNDARY_RESTRICTIONS,BOUNDARY_CONTAMINATIONS,BEGIN_DATE,END_DATE",
            "returnGeometry": "false", "f": "json"}, timeout=15).json(),
        "ic_1mi":         lambda: requests.get(ICR, params={
            "geometry": f"{lon-0.015},{lat-0.015},{lon+0.015},{lat+0.015}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "outFields": "PRIMARY_SITE_NAME,PRIMARY_SITE_ID,BOUNDARY_RESTRICTIONS,BEGIN_DATE,END_DATE",
            "returnGeometry": "false", "f": "json"}, timeout=15).json(),
        "eric":           lambda: fdep_query(ERIC_LAYER, lat, lon, 0.5, out_fields="SITE_NAME,PROGRAM,SITE_STATUS,ERIC_ID"),
        "dep_cont":       lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields=DEP_FIELDS),
        "dep_fields":     lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields="*"),
        "dep_lust":       lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=LUST_WHERE, out_fields=DEP_FIELDS),
        "dep_vol":        lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=VOL_WHERE, out_fields=DEP_FIELDS),
        "dep_brown":      lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS),
        "cleanup_sp_layers": lambda: requests.get(
            "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer",
            params={"f": "json"}, timeout=15).json(),
        "fdep_bf_areas":  lambda: requests.get(FDEP_BROWN, params={
            "where": f"LATITUDE >= {lat-0.05} AND LATITUDE <= {lat+0.05} AND LONGITUDE >= {lon-0.05} AND LONGITUDE <= {lon+0.05}",
            "outFields": "AREA_NAME,SITE_NAME,SITE_ID,REMEDIATION,LATITUDE,LONGITUDE",
            "returnGeometry": "false", "f": "json"}, timeout=15).json(),
        "fdep_bf_layer0": lambda: requests.get(
            "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/BROWNFIELD_AREAS/MapServer/0/query",
            params={"where": f"LATITUDE >= {lat-0.05} AND LATITUDE <= {lat+0.05} AND LONGITUDE >= {lon-0.05} AND LONGITUDE <= {lon+0.05}",
                    "outFields": "*", "returnGeometry": "false", "f": "json"}, timeout=15).json(),
        "fdep_bf_layer_info": lambda: requests.get(
            "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/BROWNFIELD_AREAS/MapServer/1",
            params={"f": "json"}, timeout=15).json(),
        "fdep_bf_service":lambda: requests.get(
            "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/BROWNFIELD_AREAS/MapServer",
            params={"f": "json"}, timeout=15).json(),
        "dep_super":      lambda: fdep_query(DEP_CLEANUP, lat, lon, 1.0, where=SUPER_WHERE, out_fields=DEP_FIELDS),
        "fl_superfund":   lambda: fdep_query(FL_SUPERFUND, lat, lon, 1.0, out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY"),
        "epa_brownfields":lambda: frs_spatial(FRS_ACRES, lat, lon, 0.5, out_fields="PRIMARY_NAME,LATITUDE83,LONGITUDE83,ACTIVE_STATUS,INTEREST_TYPE,REGISTRY_ID"),
        "cercla":         lambda: frs_query(FRS_SEMS, where_b, "PRIMARY_NAME,NPL_STATUS_NAME,LATITUDE83,LONGITUDE83"),
        "frs_npl":        lambda: frs_npl(lat, lon, 1.0),
        "echo_rcra_ca":   lambda: echogeo_rcra_all(lat, lon, 1.0)["ca"],
        "dep_all":        lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, out_fields=DEP_FIELDS + ",SOURCE_DATABASE_NAME,CLCC_CLEANUP_CATEGORY_KEY"),
        "echo_rcra_tsd":  lambda: echo_rcra(lat, lon, 0.5, "TSD"),
        "echo_rcra_gen":  lambda: echo_rcra(lat, lon, 0.05, "LQG,SQG,VSQG"),
        "echo_rcra_all":  lambda: echo_rcra(lat, lon, 1.0, "CA,TSD,LQG,SQG,VSQG,CESQG"),
        "echo_rcra_5mi":  lambda: echo_rcra(lat, lon, 5.0, "CA,TSD"),
        "fuds":           lambda: fuds(lat, lon, 1.0),
    }
    if db not in routes:
        return jsonify({"error": f"unknown db '{db}'", "options": list(routes.keys())}), 400
    return jsonify(routes[db]())

# ── Raw FRS test endpoint ─────────────────────────────────────────────────────
@app.route("/rawdebug", methods=["GET"])
def rawdebug():
    results = {}
    url = FRS_SEMS
    # Test 1: minimal
    try:
        r = requests.get(url, params={"where":"1=1","resultRecordCount":"1","f":"json"}, timeout=20)
        results["test1_minimal"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["test1_minimal"] = {"error": str(e)}
    # Test 2: count only
    try:
        r = requests.get(url, params={"where":"1=1","returnCountOnly":"true","f":"json"}, timeout=20)
        results["test2_count"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["test2_count"] = {"error": str(e)}
    # Test 3: get all field names from layer
    try:
        r = requests.get("https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21",
            params={"f":"json"}, timeout=20)
        data = r.json()
        fields = [f["name"] for f in data.get("fields", [])]
        results["test3_fields"] = {"status": r.status_code, "fields": fields}
    except Exception as e:
        results["test3_fields"] = {"error": str(e)}
    # Test 4: cercla function directly
    try:
        results["test4_cercla_fn"] = cercla(27.745717, -82.68471, 0.5)
    except Exception as e:
        results["test4_cercla_fn"] = {"error": str(e)}
    # Test 5: npl function directly
    try:
        results["test5_npl_fn"] = frs_npl(27.745717, -82.68471, 1.0)
    except Exception as e:
        results["test5_npl_fn"] = {"error": str(e)}
    return jsonify(results)

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    import datetime
    try:
        dl_date = datetime.date.fromisoformat(RCRA_CA_DOWNLOAD_DATE)
        age_days = (datetime.date.today() - dl_date).days
        ca_warning = f"RCRA CA data is {age_days} days old (downloaded {RCRA_CA_DOWNLOAD_DATE})"
        if age_days > 90:
            ca_warning += " — UPDATE RECOMMENDED (quarterly refresh suggested)"
    except:
        ca_warning = f"RCRA CA download date unknown"
    return jsonify({
        "status": "ok",
        "service": "Phase I ESA Proxy",
        "version": "9.101",
        "name": "Phase I ESA Proxy v9.101",
        "rcra_ca_facilities": len(RCRA_CA_DATA),
        "rcra_ca_status": ca_warning,
        "fuds_fy": FUDS_FY,
        "fuds_sites": len(FUDS_SITES)
    })

@app.route("/rcrtest", methods=["GET"])
def rcrtest():
    """Test ECHO GeoServer RCR_STATUS values — show PA facilities detail."""
    lat = float(request.args.get("lat", 27.8810756))
    lon = float(request.args.get("lon", -82.7475217))
    results = {}
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, 1.0)
    envelope = f"{mn_lon},{mn_lat},{mx_lon},{mx_lat}"

    # Get all RCRA facilities with full details
    try:
        r = requests.get(ECHOGEO_RCRA, params={
            "geometry": envelope, "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "where": "RCR_STATE='FL'",
            "outFields": "RCR_NAME,FAC_LAT,FAC_LONG,RCRA_UNIVERSE,RCR_STATUS,RCRA_CURR_COMPL_STATUS,RCRA_IDS",
            "returnGeometry": "false", "f": "json"
        }, timeout=20)
        data = r.json()
        features = data.get("features", [])
        status_values = {}
        pa_facilities = []
        for f in features:
            attrs = f["attributes"]
            rcr_s = str(attrs.get("RCR_STATUS","") or "")
            status_values[rcr_s] = status_values.get(rcr_s, 0) + 1
            if "PA" in rcr_s or " P" in rcr_s:
                pa_facilities.append({
                    "name": attrs.get("RCR_NAME"),
                    "rcr_status": rcr_s,
                    "universe": attrs.get("RCRA_UNIVERSE"),
                    "compliance": attrs.get("RCRA_CURR_COMPL_STATUS"),
                    "lat": attrs.get("FAC_LAT"),
                    "lon": attrs.get("FAC_LONG"),
                    "rcra_ids": attrs.get("RCRA_IDS")
                })
        results["total_facilities"] = len(features)
        results["rcr_status_values"] = status_values
        results["pa_facilities"] = pa_facilities
        results["echo_geo_layers"] = [{"id": l["id"], "name": l["name"]}
            for l in requests.get(
                "https://echogeo.epa.gov/arcgis/rest/services/ECHO/Facilities/MapServer",
                params={"f":"json"}, timeout=15).json().get("layers",[])]
    except Exception as e:
        results["error"] = str(e)

    return jsonify(results)

@app.route("/browndebug", methods=["GET"])
def browndebug():
    try:
        lat = float(request.args.get("lat", 27.745717))
        lon = float(request.args.get("lon", -82.68471))
    except ValueError:
        return jsonify({"error": "bad lat/lon"}), 400
    # FDEP brownfields
    fdep_raw = fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS)
    fdep_sites = parse_fdep(fdep_raw, lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
    # EPA ACRES — include INTEREST_TYPE to confirm brownfield designation
    epa_raw = frs_spatial(FRS_ACRES, lat, lon, 0.5,
        out_fields="PRIMARY_NAME,LATITUDE83,LONGITUDE83,ACTIVE_STATUS,LOCATION_ADDRESS,REGISTRY_ID,INTEREST_TYPE,PGM_SYS_ACRNM")
    epa_sites = []
    for feat in epa_raw.get("features", []):
        attrs = feat.get("attributes", {})
        epa_sites.append({
            "name":         attrs.get("PRIMARY_NAME"),
            "address":      attrs.get("LOCATION_ADDRESS"),
            "status":       attrs.get("ACTIVE_STATUS"),
            "interest_type":attrs.get("INTEREST_TYPE"),
            "program":      attrs.get("PGM_SYS_ACRNM"),
            "registry_id":  attrs.get("REGISTRY_ID"),
            "lat":          attrs.get("LATITUDE83"),
            "lon":          attrs.get("LONGITUDE83"),
        })
    return jsonify({
        "fdep_raw_count": len(fdep_raw.get("features", [])),
        "fdep_sites": fdep_sites,
        "epa_raw_count": len(epa_raw.get("features", [])),
        "epa_sites": epa_sites,
        "epa_error": epa_raw.get("error")
    })


@app.route("/superdebug", methods=["GET"])
def superdebug():
    try:
        lat = float(request.args.get("lat", 27.745717))
        lon = float(request.args.get("lon", -82.68471))
    except ValueError:
        return jsonify({"error": "bad lat/lon"}), 400
    # Layer 1 — dedicated FL Superfund layer
    layer1_raw = fdep_query(FL_SUPERFUND, lat, lon, 1.0,
        out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY,SOURCE_DATABASE_NAME")
    layer1_sites = []
    for feat in layer1_raw.get("features", []):
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        dist  = 999.0
        if geom and "x" in geom and "y" in geom:
            try: dist = round(haversine(lat, lon, float(geom["y"]), float(geom["x"])), 2)
            except: pass
        layer1_sites.append({
            "name":     attrs.get("BUSINESS_NAME"),
            "status":   attrs.get("RSC2_REMEDIATION_STATUS_KEY"),
            "category": attrs.get("CLCC_CLEANUP_CATEGORY_KEY"),
            "source_db":attrs.get("SOURCE_DATABASE_NAME"),
            "distance": dist,
            "layer":    "MapServer/1 (FL Superfund)"
        })
    # Layer 0 — DEP Cleanup filtered to SUPER/OTHCU
    layer0_raw = fdep_query(DEP_CLEANUP, lat, lon, 1.0, where=SUPER_WHERE,
        out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY,SOURCE_DATABASE_NAME")
    layer0_sites = []
    for feat in layer0_raw.get("features", []):
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        dist  = 999.0
        if geom and "x" in geom and "y" in geom:
            try: dist = round(haversine(lat, lon, float(geom["y"]), float(geom["x"])), 2)
            except: pass
        layer0_sites.append({
            "name":     attrs.get("BUSINESS_NAME"),
            "status":   attrs.get("RSC2_REMEDIATION_STATUS_KEY"),
            "category": attrs.get("CLCC_CLEANUP_CATEGORY_KEY"),
            "source_db":attrs.get("SOURCE_DATABASE_NAME"),
            "distance": dist,
            "layer":    "MapServer/0 (DEP Cleanup SUPER/OTHCU)"
        })
    return jsonify({
        "layer1_count": len(layer1_sites),
        "layer1_sites": sorted(layer1_sites, key=lambda x: x["distance"]),
        "layer0_count": len(layer0_sites),
        "layer0_sites": sorted(layer0_sites, key=lambda x: x["distance"]),
    })


@app.route("/fudsdebug", methods=["GET"])
def fudsdebug():
    """Test multiple FUDS URL candidates to find the working one."""
    results = {}
    # Test the original URL with centroid_x/centroid_y fields instead of LATITUDE/LONGITUDE
    url = "https://services7.arcgis.com/n1YM8pTrFmm7L4hs/arcgis/rest/services/FUDS_Projects/FeatureServer/0/query"
    results = {}

    # Test 1: get one FL record with all fields to see what's available
    try:
        r = requests.get(url, params={
            "where": "STATE_CODE='FL'",
            "outFields": "*",
            "resultRecordCount": "1",
            "returnGeometry": "true",
            "f": "json"
        }, timeout=15)
        results["sample_record"] = r.json()
    except Exception as e:
        results["sample_error"] = str(e)

    # Test 2: WHERE on centroid_x/centroid_y
    try:
        r = requests.get(url, params={
            "where": "centroid_x >= -82.75 AND centroid_x <= -82.70 AND centroid_y >= 28.00 AND centroid_y <= 28.07",
            "outFields": "PROPERTY_NAME,PROJECT_STATUS,STATE_CODE,centroid_x,centroid_y",
            "returnGeometry": "false",
            "f": "json"
        }, timeout=15)
        results["centroid_where"] = r.json()
    except Exception as e:
        results["centroid_error"] = str(e)

    # Test 3: spatial query with geometry
    try:
        r = requests.get(url, params={
            "geometry": "-82.75,28.00,-82.70,28.07",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326",
            "outSR": "4326",
            "outFields": "PROPERTY_NAME,PROJECT_STATUS,STATE_CODE",
            "returnGeometry": "true",
            "f": "json"
        }, timeout=15)
        results["spatial_query"] = r.json()
    except Exception as e:
        results["spatial_error"] = str(e)

    candidates = []  # unused now
    return jsonify(results)


@app.route("/frs_lookup", methods=["GET"])
def frs_lookup():
    """Look up a facility by FRS registry ID."""
    registry_id = request.args.get("id", "")
    if not registry_id:
        return jsonify({"error": "id parameter required"}), 400
    results = {}
    # Query FRS all interests for this registry ID
    try:
        r = requests.get(
            "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/8/query",
            params={
                "where": f"REGISTRY_ID='{registry_id}'",
                "outFields": "*",
                "returnGeometry": "false",
                "f": "json"
            }, timeout=15)
        data = r.json()
        results["frs_interests"] = {
            "count": len(data.get("features",[])),
            "features": [f["attributes"] for f in data.get("features",[])]
        }
    except Exception as e:
        results["frs_interests"] = {"error": str(e)}
    # Also check FRS RCRA layer
    try:
        r = requests.get(FRS_RCRA, params={
            "where": f"REGISTRY_ID='{registry_id}'",
            "outFields": "PRIMARY_NAME,ACTIVE_STATUS,LATITUDE83,LONGITUDE83,REGISTRY_ID",
            "returnGeometry": "false", "f": "json"
        }, timeout=15)
        data = r.json()
        results["frs_rcra"] = {
            "count": len(data.get("features",[])),
            "features": [f["attributes"] for f in data.get("features",[])]
        }
    except Exception as e:
        results["frs_rcra"] = {"error": str(e)}
    return jsonify(results)


@app.route("/find_ca", methods=["GET"])
def find_ca():
    """Look up Honeywell by handler ID to see what coords ECHO has."""
    handler_id = request.args.get("id", "FLD004104105")
    state = request.args.get("state", "FL")
    results = {}

    # Look up by handler ID directly
    try:
        r = requests.get("https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info", params={
            "output": "JSON",
            "p_id": handler_id,
            "qcolumns": "1,2,3,4,5,6,38,39,40"
        }, timeout=20)
        data = r.json()
        facilities = data.get("Results", {}).get("Facilities", [])
        results["by_handler_id"] = {
            "status": r.status_code,
            "count": len(facilities),
            "facilities": [{"name": f.get("FacName"), "lat": f.get("FacLat"),
                           "lon": f.get("FacLong"), "city": f.get("FacCity"),
                           "type": f.get("RCRAHandlerType"),
                           "compliance": f.get("RCRAComplianceStatus")}
                          for f in facilities]
        }
    except Exception as e:
        results["by_handler_id_error"] = str(e)

    # Also try large radius search near Honeywell
    try:
        r = requests.get("https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info", params={
            "output": "JSON",
            "p_lat": 27.890774,
            "p_lon": -82.720478,
            "p_radius_mi": 5.0,
            "qcolumns": "1,2,3,4,5,6,38,39,40"
        }, timeout=20)
        data = r.json()
        facilities = data.get("Results", {}).get("Facilities", [])
        results["5mi_radius"] = {
            "status": r.status_code,
            "count": len(facilities),
            "facilities": [{"name": f.get("FacName"), "lat": f.get("FacLat"),
                           "lon": f.get("FacLong"), "type": f.get("RCRAHandlerType")}
                          for f in facilities[:5]]
        }
    except Exception as e:
        results["5mi_radius_error"] = str(e)

    return jsonify(results)


@app.route("/find_ca_old", methods=["GET"])
def find_ca_old():
    """Find RCRA CA facilities in Florida by trying multiple ECHO parameters."""
    state = request.args.get("state", "FL")
    results = {}

    # Attempt 1: p_htype=TSD (TSD facilities are most likely to have CA)
    try:
        r = requests.get("https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info", params={
            "output": "JSON", "p_state": state, "p_htype": "TSD",
            "qcolumns": "1,2,3,4,5,6,38,39,40", "responseset": "10"
        }, timeout=20)
        data = r.json()
        facilities = data.get("Results", {}).get("Facilities", [])
        results["tsd_facilities"] = [{"name": f.get("FacName"), "lat": f.get("FacLat"),
                                      "lon": f.get("FacLong"), "city": f.get("FacCity"),
                                      "compliance": f.get("RCRAComplianceStatus"),
                                      "handler_type": f.get("RCRAHandlerType")} 
                                     for f in facilities[:10]]
        results["tsd_status"] = r.status_code
    except Exception as e:
        results["tsd_error"] = str(e)

    # Attempt 2: no filter, get any FL RCRA facility to see field names
    try:
        r = requests.get("https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info", params={
            "output": "JSON", "p_state": state,
            "qcolumns": "1,2,3,4,5,6,38,39,40,41,42,43", "responseset": "3"
        }, timeout=20)
        data = r.json()
        facilities = data.get("Results", {}).get("Facilities", [])
        # Show all fields for first facility
        results["sample_facility"] = facilities[0] if facilities else None
        results["any_status"] = r.status_code
    except Exception as e:
        results["any_error"] = str(e)

    return jsonify(results)


@app.route("/clearcache", methods=["GET"])
def clearcache():
    """Clear the RCRA CA cache."""
    with _rcra_cache_lock:
        count = len(_rcra_cache)
        _rcra_cache.clear()
    return jsonify({"cleared": count, "message": f"Cleared {count} cached grid cells"})


@app.route("/nexus_docs", methods=["GET"])
def nexus_docs():
    """Fetch FDEP Nexus documents for a facility and return the best contamination report."""
    facility_id = request.args.get("id", "")
    if not facility_id:
        return jsonify({"error": "id parameter required"}), 400

    PRIORITY_TYPES = {
        'SITE ASSESSMENT RELATED': 10,
        'OPERATION AND MAINT - REMEDIAL ACTION RPT RELATED': 10,
        'MONITORING PLANS AND REPORTS RELATED': 8,
        'REMEDIAL ACTION PLAN RELATED': 9,
        'SOURCE REMOVAL RELATED': 7,
        'COMPLETION RELATED': 7,
        'LAB ANALYTICAL REPORTS': 5,
        'POTABLE WELL SURVEY - SAMPLING': 5,
        'DISCHARGE REPORTING RELATED': 4,
    }
    GOOD_SUBJECTS_EXTRA = ['RAGR','GENERAL REMEDIAL ACTION','QUARTERLY REPORT',
                           'Q1','Q2','Q3','Q4','ANNUAL REPORT']
    GOOD_SUBJECTS = ['ASSESSMENT REPORT','SITE ASSESSMENT','CONTAMINATION ASSESSMENT','RAGR','GENERAL REMEDIAL ACTION','QUARTERLY REMEDIAL',
                     'MONITORING REPORT','REMEDIAL ACTION','CLOSURE REPORT',
                     'GROUNDWATER','SOIL ASSESSMENT','INTERIM ASSESSMENT']
    BAD_SUBJECTS  = ['INVOICE','RATE SHEET','HASP','CONFIRMATION','UPLOAD',
                     'ZIP','NOTIFICATION','RECEIPT','CHECKLIST']

    def parse_date(d):
        from datetime import datetime
        try: return datetime.strptime(d.strip(), '%m-%d-%Y')
        except: return datetime.min

    def score_doc(row):
        score = PRIORITY_TYPES.get(row.get('DOCUMENT TYPE',''), 0)
        subj = row.get('SUBJECT','').upper()
        for kw in GOOD_SUBJECTS:
            if kw in subj: score += 2
        for kw in BAD_SUBJECTS:
            if kw in subj: score -= 3
        return score

    try:
        import csv, io
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/csv,*/*",
            "Referer": (f"https://prodenv.dep.state.fl.us/DepNexus/public/"
                       f"electronic-documents/{facility_id}/facility!search")
        }
        rows = []
        page = 1
        while True:
            url = (f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents"
                   f"/{facility_id}/export?wildCardMatch=false&page={page}")
            r = requests.get(url, timeout=20, headers=headers)
            r.raise_for_status()
            text = r.text.strip()
            if not text: break
            reader = csv.DictReader(io.StringIO(text))
            page_rows = list(reader)
            if not page_rows: break
            rows.extend(page_rows)
            # If fewer than 100 rows returned, probably last page
            if len(page_rows) < 100: break
            page += 1
            if page > 20: break  # safety limit
    except Exception as e:
        return jsonify({"error": str(e), "facility_id": facility_id})

    # Filter to priority types only
    candidates = [row for row in rows if row.get('DOCUMENT TYPE','') in PRIORITY_TYPES]
    if not candidates:
        return jsonify({
            "facility_id": facility_id,
            "total_docs": len(rows),
            "best_doc": None,
            "message": "No priority documents found — see Nexus for full list"
        })

    # Score and sort
    candidates.sort(key=lambda r: (score_doc(r), parse_date(r.get('DOCUMENT DATE',''))), reverse=True)
    best = candidates[0]

    return jsonify({
        "facility_id": facility_id,
        "total_docs": len(rows),
        "best_doc": {
            "date": best.get('DOCUMENT DATE',''),
            "type": best.get('DOCUMENT TYPE',''),
            "subject": best.get('SUBJECT',''),
            "url": best.get('FILE PATH',''),
            "file_type": best.get('FILE TYPE',''),
            "file_size": best.get('FILE SIZE',''),
        },
        "top5": [{
            "date": r.get('DOCUMENT DATE',''),
            "type": r.get('DOCUMENT TYPE',''),
            "subject": r.get('SUBJECT',''),
            "url": r.get('FILE PATH',''),
            "score": score_doc(r)
        } for r in candidates[:5]]
    })


@app.route("/nexus_search", methods=["GET"])
def nexus_search():
    """Test Nexus pagination formats and search for document."""
    facility_id = request.args.get("id", "9045812")
    search = request.args.get("q", "5140103").upper()
    results = {}

    try:
        import csv, io
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents/{facility_id}/facility!search"
        }
        # Test page 1 - count rows and show first/last
        r1 = requests.get(
            f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents/{facility_id}/export?wildCardMatch=false&page=1",
            timeout=20, headers=hdrs)
        rows1 = list(csv.DictReader(io.StringIO(r1.text)))
        results["page1_count"] = len(rows1)
        results["page1_first"] = {"date": rows1[0].get("DOCUMENT DATE"), "subject": rows1[0].get("SUBJECT")} if rows1 else None
        results["page1_last"] = {"date": rows1[-1].get("DOCUMENT DATE"), "subject": rows1[-1].get("SUBJECT")} if rows1 else None

        # Test page 2
        r2 = requests.get(
            f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents/{facility_id}/export?wildCardMatch=false&page=2",
            timeout=20, headers=hdrs)
        rows2 = list(csv.DictReader(io.StringIO(r2.text)))
        results["page2_count"] = len(rows2)
        results["page2_first"] = {"date": rows2[0].get("DOCUMENT DATE"), "subject": rows2[0].get("SUBJECT")} if rows2 else None

        # Try rowOffset pagination
        r_offset = requests.get(
            f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents/{facility_id}/export?wildCardMatch=false&rowOffset=100",
            timeout=20, headers=hdrs)
        rows_off = list(csv.DictReader(io.StringIO(r_offset.status_code == 200 and r_offset.text or "")))
        results["offset100_count"] = len(rows_off)
        results["offset100_first"] = {"date": rows_off[0].get("DOCUMENT DATE"), "subject": rows_off[0].get("SUBJECT")} if rows_off else None

        # Check if page1 same as page2
        results["page1_same_as_page2"] = (rows1 == rows2)
        results["total_docs_from_page1"] = len(rows1)

    except Exception as e:
        results["error"] = str(e)
    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
