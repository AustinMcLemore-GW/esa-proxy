"""
Phase I ESA Database Proxy — v9.61
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
try:
    with open(_FUDS_FILE) as _f:
        _FUDS_DATA = json.load(_f)
    FUDS_SITES = _FUDS_DATA["sites"]
    FUDS_FY    = _FUDS_DATA["fiscal_year"]
except Exception as _e:
    FUDS_SITES = []
    FUDS_FY    = "unknown"
    print(f"WARNING: Could not load FUDS data: {_e}")

app = Flask(__name__, static_folder='.', static_url_path='')
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
        sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
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
        sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
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
DEP_FIELDS  = "BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY,SOURCE_DATABASE_NAME"
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


def echogeo_rcra_all(lat, lon, radius_miles):
    """
    Query RCRA facilities from ECHO GeoServer — no rate limiting.
    Returns ca, tsd, gen dicts filtered by radius and type.
    """
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, radius_miles)
    envelope = f"{mn_lon},{mn_lat},{mx_lon},{mx_lat}"
    fields = ("RCR_NAME,RCR_CITY,RCR_STATE,FAC_LAT,FAC_LONG,"
              "RCRA_UNIVERSE,RCRA_CURR_COMPL_STATUS,RCRA_CURR_SNC,RCR_STATUS")
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
        attrs = feat.get("attributes", {})
        flat = float(attrs.get("FAC_LAT", 0) or 0)
        flon = float(attrs.get("FAC_LONG", 0) or 0)
        if not flat or not flon: continue
        dist = round(haversine(lat, lon, flat, flon), 2)
        name = str(attrs.get("RCR_NAME","Unknown") or "Unknown")
        universe = str(attrs.get("RCRA_UNIVERSE","") or "").upper()
        status = str(attrs.get("RCRA_CURR_COMPL_STATUS","") or "")
        snc = str(attrs.get("RCRA_CURR_SNC","") or "")
        site = {"name": name, "distance": dist, "status": status, "nc": False}
        if "CA" in universe:
            if dist <= radius_miles:
                site["nc"] = status not in ["No Violation Identified",""] or snc == "Yes"
                ca_sites.append(dict(site))
        if any(t in universe for t in ["TSD","TSDF"]):
            if dist <= 0.5:
                tsd_sites.append(dict(site))
        if any(t in universe for t in ["LQG","SQG","VSQG","CESQG"]):
            if dist <= 0.05:
                gen_sites.append(dict(site))
    return {
        "ca":  {"count": len(ca_sites),  "sites": sorted(ca_sites,  key=lambda s: s["distance"])},
        "tsd": {"count": len(tsd_sites), "sites": sorted(tsd_sites, key=lambda s: s["distance"])},
        "gen": {"count": len(gen_sites), "sites": sorted(gen_sites, key=lambda s: s["distance"])},
    }


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
        fdep_coords = [(float(f["geometry"]["y"]), float(f["geometry"]["x"]))
                       for f in fdep_raw_brown.get("features", [])
                       if f.get("geometry") and "x" in f["geometry"]]
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
        # Drop EPA ACRES sites sharing significant name overlap with FDEP sites
        stopwords = {"THE","OF","A","AN","AND","AT","IN","INC","LLC","CORP","SITE",
                     "PART","CLASS","III","II","I","CLOSED","AVE","ST","RD","BLVD"}
        fdep_keywords = [set(s["name"].upper().replace("-"," ").split()) - stopwords
                         for s in fdep_sites]
        filtered_epa = []
        for epa in epa_sites:
            epa_words = set(epa["name"].upper().replace("-"," ").split()) - stopwords
            is_duplicate = any(len(epa_words & fk) >= 2 for fk in fdep_keywords)
            if not is_duplicate:
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

    task_map = {
        "npl":            lambda: frs_npl(lat, lon, 1.0, status_filter=["Currently on the Final NPL","Proposed for NPL"]),
        "fuds":           lambda: fuds(lat, lon, 1.0),
        "rcra_ca":        lambda: echogeo_results["ca"],
        "state_superfund":get_state_superfund,
        "npl_del":        get_delisted,
        "cercla":         lambda: cercla(lat, lon, 0.5),
        "rcra_tsd":       lambda: echogeo_results["tsd"],
        "haz":            get_haz,
        "cont":           lambda: (lambda s: {"count": len(s), "sites": s})(merge_dedup(
                              parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields=DEP_FIELDS), lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC),
                              eric_query(lat, lon, 0.5, program_filter=["State Funded Cleanup Program","Site Investigation Section","Hazardous Waste Cleanup Program","CERCLA Site Screening Program","State and Tribal Response Program","State-owned Lands Cleanup Program"]))),
        "solid":          lambda: (lambda sites: {"count": len(sites), "sites": sites})(
                              list({s["name"]: s for s in sorted(
                                  parse_fdep(fdep_query(SOLID_WASTE, lat, lon, 0.5,
                                      where="FACILITY_STATUS NOT IN ('Closed','Inactive','CLOSED','INACTIVE','Closed, No Gw Monitoring','Closed, Gw Monitoring')",
                                      out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE"),
                                      lat, lon, "FACILITY_NAME", "FACILITY_STATUS", set()),
                                  key=lambda x: x["distance"])}.values())),  # deduplicated by name
        "lust":           get_lust,
        "vol":            lambda: (lambda s: {"count": len(s), "sites": s})(merge_dedup(
                              parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=VOL_WHERE, out_fields=DEP_FIELDS), lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC),
                              eric_query(lat, lon, 0.5, program_filter=["Drycleaning Solvent Cleanup Program","Responsible Party Cleanup"]))),
        "brown":          get_brownfields,
        "ust":            lambda: mk(None)(parse_fdep(fdep_query(STCM_TANKS, lat, lon, 0.05, out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_CLEANUP_STATUS"), lat, lon, "FACILITY_NAME", "FACILITY_STATUS", {"Active","ACTIVE","Open","OPEN"})),
        "rcra_gen":       lambda: echogeo_results["gen"],
        "ic":             lambda: mk(None)(parse_fdep(fdep_query(ICR, lat, lon, 0.05, out_fields="SITE_NAME,IC_STATUS,MECHANISM_TYPE"), lat, lon, "SITE_NAME", "IC_STATUS", {"ACTIVE","Active"})),
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
    # in the highest-priority category. Priority: brown > state_superfund > cont > vol > lust
    dedup_priority = ["state_superfund", "brown", "lust", "cont", "vol"]
    seen_globally = set()
    for category in dedup_priority:
        if category not in res or "sites" not in res[category]:
            continue
        filtered = []
        for site in res[category].get("sites", []):
            name_key = site["name"].strip().upper()
            if name_key not in seen_globally:
                seen_globally.add(name_key)
                filtered.append(site)
        res[category]["sites"] = filtered
        res[category]["count"] = len(filtered)

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
        "ic":             lambda: fdep_query(ICR, lat, lon, 0.05, out_fields="SITE_NAME,IC_STATUS,MECHANISM_TYPE"),
        "eric":           lambda: fdep_query(ERIC_LAYER, lat, lon, 0.5, out_fields="SITE_NAME,PROGRAM,SITE_STATUS,ERIC_ID"),
        "dep_cont":       lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields=DEP_FIELDS),
        "dep_lust":       lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=LUST_WHERE, out_fields=DEP_FIELDS),
        "dep_vol":        lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=VOL_WHERE, out_fields=DEP_FIELDS),
        "dep_brown":      lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS),
        "dep_super":      lambda: fdep_query(DEP_CLEANUP, lat, lon, 1.0, where=SUPER_WHERE, out_fields=DEP_FIELDS),
        "fl_superfund":   lambda: fdep_query(FL_SUPERFUND, lat, lon, 1.0, out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY"),
        "epa_brownfields":lambda: frs_query(FRS_ACRES, where_b, "PRIMARY_NAME,LATITUDE83,LONGITUDE83,SITE_STATUS"),
        "cercla":         lambda: frs_query(FRS_SEMS, where_b, "PRIMARY_NAME,NPL_STATUS_NAME,LATITUDE83,LONGITUDE83"),
        "frs_npl":        lambda: frs_npl(lat, lon, 1.0),
        "echo_rcra_ca":   lambda: echo_rcra(lat, lon, 1.0, "CA"),
        "echo_rcra_tsd":  lambda: echo_rcra(lat, lon, 0.5, "TSD"),
        "echo_rcra_gen":  lambda: echo_rcra(lat, lon, 0.05, "LQG,SQG,VSQG"),
        "echo_rcra_all":  lambda: echo_rcra(lat, lon, 1.0, "CA,TSD,LQG,SQG,VSQG,CESQG"),
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
    return jsonify({"status": "ok", "service": "Phase I ESA Proxy", "version": "9.61", "name": "Phase I ESA Proxy v9.61"})

@app.route("/rcrtest", methods=["GET"])
def rcrtest():
    """Test ECHO GeoServer RCRA universe values in search area."""
    lat = float(request.args.get("lat", 27.808116))
    lon = float(request.args.get("lon", -82.665444))
    results = {}
    base = "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/NationalRCRABoundaries/FeatureServer/1/query"
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, 1.0)

    # Test 1: envelope spatial query with FL filter
    try:
        r = requests.get(base, params={
            "geometry": f"{mn_lon},{mn_lat},{mx_lon},{mx_lat}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "where": "LOCATION_STATE='FL'",
            "outFields": "HANDLER_ID,HANDLER_NAME,LOCATION_CITY,LOCATION_STATE",
            "returnGeometry": "false", "f": "json"
        }, timeout=15)
        data = r.json()
        results["envelope_with_fl"] = {
            "status": r.status_code,
            "count": len(data.get("features",[])),
            "error": data.get("error"),
            "sites": [f["attributes"] for f in data.get("features",[])]
        }
    except Exception as e:
        results["envelope_with_fl"] = {"error": str(e)}

    # Test 2: envelope spatial query NO state filter
    try:
        r = requests.get(base, params={
            "geometry": f"{mn_lon},{mn_lat},{mx_lon},{mx_lat}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4326", "outSR": "4326",
            "outFields": "HANDLER_ID,HANDLER_NAME,LOCATION_CITY,LOCATION_STATE",
            "returnGeometry": "false", "f": "json"
        }, timeout=15)
        data = r.json()
        results["envelope_no_filter"] = {
            "status": r.status_code,
            "count": len(data.get("features",[])),
            "error": data.get("error"),
            "sites": [f["attributes"] for f in data.get("features",[])]
        }
    except Exception as e:
        results["envelope_no_filter"] = {"error": str(e)}

    # Test 3: FL facilities WITH geometry to check coordinates
    try:
        r = requests.get(base, params={
            "where": "LOCATION_STATE='FL'",
            "resultRecordCount": "3",
            "outFields": "HANDLER_ID,HANDLER_NAME,LOCATION_CITY,LOCATION_STATE",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json"
        }, timeout=15)
        data = r.json()
        # Extract centroid of each polygon
        samples = []
        for feat in data.get("features", []):
            attrs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            rings = geom.get("rings", []) if geom else []
            centroid = None
            if rings and rings[0]:
                pts = rings[0]
                centroid = [sum(p[0] for p in pts)/len(pts), sum(p[1] for p in pts)/len(pts)]
            samples.append({"name": attrs.get("HANDLER_NAME"), "city": attrs.get("LOCATION_CITY"),
                           "centroid": centroid, "has_geometry": bool(rings)})
        results["fl_with_geometry"] = {
            "count": len(data.get("features",[])),
            "error": data.get("error"),
            "samples": samples
        }
    except Exception as e:
        results["fl_with_geometry"] = {"error": str(e)}

    # Test 4: ECHO GeoServer RCRA layer
    try:
        r = requests.get(
            "https://echogeo.epa.gov/arcgis/rest/services/ECHO/Facilities/MapServer/3",
            params={"f": "json"}, timeout=15)
        data = r.json()
        results["echogeo_layer_info"] = {
            "name": data.get("name"),
            "fields": [f["name"] for f in data.get("fields",[])[:15]],
            "extent": str(data.get("extent",""))[:100]
        }
    except Exception as e:
        results["echogeo_layer_info"] = {"error": str(e)}

    # Test: get all RCRA universe values in area
    try:
        r = requests.get(
            "https://echogeo.epa.gov/arcgis/rest/services/ECHO/Facilities/MapServer/3/query",
            params={
                "geometry": f"{mn_lon},{mn_lat},{mx_lon},{mx_lat}",
                "geometryType": "esriGeometryEnvelope",
                "spatialRel": "esriSpatialRelIntersects",
                "inSR": "4326", "outSR": "4326",
                "where": "RCR_STATE='FL'",
                "outFields": "RCR_NAME,FAC_LAT,FAC_LONG,RCRA_UNIVERSE,RCRA_CURR_COMPL_STATUS,RCRA_IDS",
                "returnGeometry": "false", "f": "json"
            }, timeout=15)
        data = r.json()
        features = data.get("features", [])
        universes = {}
        for f in features:
            u = f["attributes"].get("RCRA_UNIVERSE","unknown")
            universes[u] = universes.get(u, 0) + 1
        results["echogeo_universes"] = {
            "total": len(features),
            "universe_counts": universes,
            "all_sites": [{"name": f["attributes"].get("RCR_NAME"),
                           "universe": f["attributes"].get("RCRA_UNIVERSE"),
                           "compliance": f["attributes"].get("RCRA_CURR_COMPL_STATUS"),
                           "rcra_ids": f["attributes"].get("RCRA_IDS")}
                          for f in features]
        }
    except Exception as e:
        results["echogeo_universes"] = {"error": str(e)}

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
