"""
Phase I ESA Database Proxy — v9.114
FUDS envelope query + dedup, ERIC layer 8 integration, responsible party → voluntary cleanup.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, math, time, json, os

# ── FUDS static data (FY2024) ─────────────────────────────────────────────────
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

import threading
_rcra_cache = {}
_rcra_cache_lock = threading.Lock()
RCRA_CACHE_TTL = 86400

def _cache_key(lat, lon):
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

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def bbox(lat, lon, radius_miles):
    dlat = radius_miles / 69.0
    dlon = radius_miles / (69.0 * math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon

def fdep_query(url, lat, lon, radius_miles, where="1=1", out_fields="*"):
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
    seen = set(); out = []
    for s in sorted(list1 + list2, key=lambda x: x["distance"]):
        if s["name"] not in seen:
            seen.add(s["name"]); out.append(s)
    return out

def frs_spatial(url, lat, lon, radius_miles, out_fields="*"):
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, radius_miles)
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
CIMC_RCRA_CA  = "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/NationalRCRABoundaries/FeatureServer/1/query"
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
ERIC_NC = {"OPEN","ONHOLD","INPROCESS"}

def echo_rcra(lat, lon, radius_miles, handler_types):
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
    return echogeo_rcra_all(lat, lon, radius_miles)


def static_rcra_ca(lat, lon, radius_miles):
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
            "nc": True
        })
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}


def echogeo_rcra_all(lat, lon, radius_miles):
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

        import re as _re
        flag_match = _re.search(r'\(([^)]+)\)', rcr_status)
        flag_str = flag_match.group(1).strip() if flag_match else ""
        flags = set(flag_str.split())

        is_ca = any(f == "A" or f.endswith("A") for f in flags) or "A" in flag_str
        if is_ca and dist <= radius_miles:
            nc = status not in ["No Violation Identified",""] or snc == "Yes"
            ca_site = dict(site)
            ca_site["nc"] = nc
            ca_sites.append(ca_site)

        is_tsd = (any("P" in f for f in flags) or
                  any(t in universe for t in ["TSD","TSDF","LEGACY TSDF"]))
        if is_tsd and dist <= 0.5:
            tsd_site = dict(site)
            tsd_site["nc"] = status not in ["No Violation Identified", ""]
            tsd_sites.append(tsd_site)

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
    fields = "PRIMARY_NAME,ACTIVE_STATUS,LATITUDE83,LONGITUDE83"
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

    return {
        "ca":  {"count": 0, "sites": [], "note": "CA requires ECHO compliance data"},
        "tsd": {"count": len(tsd_sites), "sites": sorted(tsd_sites, key=lambda s: s["distance"])},
        "gen": {"count": len(gen_sites), "sites": sorted(gen_sites, key=lambda s: s["distance"])},
    }


def echo_rcra_all(lat, lon):
    url = "https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info"
    params = {
        "output":      "JSON",
        "p_lat":       lat,
        "p_lon":       lon,
        "p_radius_mi": 1.0,
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
                time.sleep(5 * attempt)
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
                if dist <= 1.0 and "CA" in htype:
                    nc = status not in ["No Violation Identified",""]
                    ca_sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
                if dist <= 0.5 and any(t in htype for t in ["TSD","TSDF","LQTSDF"]):
                    tsd_sites.append({"name": name, "distance": dist, "status": status, "nc": False})
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

def fuds(lat, lon, radius_miles):
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

def frs_npl(lat, lon, radius_miles, status_filter=None):
    data = frs_spatial(FRS_SEMS_NPL, lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,ACTIVE_STATUS,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        active   = str(attrs.get("ACTIVE_STATUS","") or "")
        flat = float(attrs.get("LATITUDE83", 0) or 0)
        flon = float(attrs.get("LONGITUDE83", 0) or 0)
        if not flat or not flon:
            continue
        dist = haversine(lat, lon, flat, flon)
        if dist > radius_miles:
            continue
        status = active
        nc = active.upper() not in {"DELETED FROM THE FINAL NPL"}
        if status_filter and active not in status_filter:
            continue
        sites.append({"name": str(attrs.get("PRIMARY_NAME","Unknown")),
                      "distance": round(dist,2), "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

def cercla(lat, lon, radius_miles):
    data = frs_spatial(FRS_SEMS, lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,INTEREST_TYPE,ACTIVE_STATUS,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = []
    for feat in data.get("features", []):
        attrs    = feat.get("attributes", {})
        interest = str(attrs.get("INTEREST_TYPE","") or "")
        active   = str(attrs.get("ACTIVE_STATUS","") or "")
        if interest not in {"SUPERFUND (NON-NPL)","SUPERFUND NON-NPL","CERCLA","NOT ON THE NPL"}:
            continue
        flat = float(attrs.get("LATITUDE83", 0) or 0)
        flon = float(attrs.get("LONGITUDE83", 0) or 0)
        if not flat or not flon:
            continue
        dist = haversine(lat, lon, flat, flon)
        if dist > radius_miles:
            continue
        nc = active.upper() in {"REMOVAL ACTION UNDERWAY", "REMOVAL ACTION COMPLETE - REMEDIAL ACTION UNDERWAY"}
        sites.append({"name": str(attrs.get("PRIMARY_NAME","Unknown")),
                      "distance": round(dist,2), "status": active, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

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
            nc = False
            raw.append({"name": name, "distance": dist, "status": str(attrs.get("FAC_INS_TYPE","") or ""), "nc": nc})
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
        s1 = parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=LUST_WHERE, out_fields=DEP_FIELDS),
            lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
        s2 = parse_fdep(fdep_query(STCM_LUST, lat, lon, 0.5, out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE"),
            lat, lon, "SITE_NAME", "SITE_STATUS", {"OPEN","ACTIVE","Active","Open"})
        s3 = eric_query(lat, lon, 0.5, program_filter=["Petroleum Restoration Program"])
        all_sites = merge_dedup(s1 + s2, s3)
        return {"count": len(all_sites), "sites": all_sites}

    def get_brownfields():
        fdep_raw_brown = fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS)
        fdep_sites = parse_fdep(fdep_raw_brown, lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)

        bf_coords = []
        bf_names  = []
        for f in fdep_raw_brown.get("features", []):
            geom = f.get("geometry", {})
            name = str(f.get("attributes", {}).get("BUSINESS_NAME","") or "")
            if geom and "x" in geom and "y" in geom:
                bf_coords.append((float(geom["y"]), float(geom["x"])))
                bf_names.append(name)

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

        seen_bf = set()

        def coord_is_dup(slat, slon, sname, threshold=0.15):
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
                    if name_sig & sig(cname):
                        return True
            return False

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

        for feat in fdep_bf_area_data.get("features", []):
            attrs = feat.get("attributes", {})
            area_id = str(attrs.get("AREA_ID","") or "")
            name    = str(attrs.get("AREA_NAME","Unknown BF Area") or "Unknown BF Area")
            flat    = float(attrs.get("LATITUDE", 0) or 0)
            flon    = float(attrs.get("LONGITUDE", 0) or 0)
            if not flat or not flon: continue
            dist = round(haversine(lat, lon, flat, flon), 2)
            if dist > 0.5: continue
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

        filtered_epa = []
        for epa in epa_sites:
            if not coord_is_dup(epa["_lat"], epa["_lon"], epa["name"]):
                bf_coords.append((epa["_lat"], epa["_lon"]))
                bf_names.append(epa["name"])
                filtered_epa.append(epa)
        for s in filtered_epa:
            s.pop("_lat", None); s.pop("_lon", None)
        seen = set(); out = []
        for s in sorted(fdep_sites + filtered_epa, key=lambda x: x["distance"]):
            if s["name"] not in seen: seen.add(s["name"]); out.append(s)
        return {"count": len(out), "sites": out}

    echogeo_results = echogeo_rcra_all(lat, lon, 1.0)
    if echogeo_results["tsd"].get("error") or echogeo_results["gen"].get("error"):
        frs_rcra_results = frs_rcra_all(lat, lon)
        echogeo_results["tsd"] = frs_rcra_results["tsd"]
        echogeo_results["gen"] = frs_rcra_results["gen"]

    def get_solid():
        data = fdep_query(SOLID_WASTE, lat, lon, 0.5,
            where="FACILITY_STATUS NOT IN ('Closed','CLOSED','Closed, No Gw Monitoring')",
            out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE")
        sites = parse_fdep(data, lat, lon, "FACILITY_NAME", "FACILITY_STATUS", set())
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
        "dep_cont":  lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields=DEP_FIELDS),
        "dep_lust":  lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=LUST_WHERE, out_fields=DEP_FIELDS),
        "stcm_lust": lambda: fdep_query(STCM_LUST, lat, lon, 0.5, out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE"),
        "dep_vol":   lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=VOL_WHERE, out_fields=DEP_FIELDS),
        "dep_brown": lambda: fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS),
        "dep_super": lambda: fdep_query(DEP_CLEANUP, lat, lon, 1.0, where=SUPER_WHERE, out_fields=DEP_FIELDS),
        "eric":      lambda: fdep_query(ERIC_LAYER, lat, lon, 0.5, out_fields="SITE_NAME,PROGRAM,SITE_STATUS,ERIC_ID"),
        "chaz":      lambda: fdep_query(CHAZ, lat, lon, 0.5, out_fields="ME_NAME,FAC_INS_TYPE,GENERATOR,PERMITTED_CONSENTED"),
        "stcm_tanks":lambda: fdep_query(STCM_TANKS, lat, lon, 0.05, out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_CLEANUP_STATUS"),
        "solid":     lambda: fdep_query(SOLID_WASTE, lat, lon, 0.5, out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE"),
        "fuds":      lambda: fuds(lat, lon, 1.0),
        "echo_rcra_ca": lambda: echogeo_rcra_all(lat, lon, 1.0)["ca"],
        "frs_npl":   lambda: frs_npl(lat, lon, 1.0),
        "cercla":    lambda: cercla(lat, lon, 0.5),
    }
    if db not in routes:
        return jsonify({"error": f"unknown db '{db}'", "options": list(routes.keys())}), 400
    return jsonify(routes[db]())


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
        "version": "9.114",
        "name": "Phase I ESA Proxy v9.114",
        "rcra_ca_facilities": len(RCRA_CA_DATA),
        "rcra_ca_status": ca_warning,
        "fuds_fy": FUDS_FY,
        "fuds_sites": len(FUDS_SITES)
    })


@app.route("/clearcache", methods=["GET"])
def clearcache():
    with _rcra_cache_lock:
        count = len(_rcra_cache)
        _rcra_cache.clear()
    return jsonify({"cleared": count, "message": f"Cleared {count} cached grid cells"})


@app.route("/rcrtest", methods=["GET"])
def rcrtest():
    lat = float(request.args.get("lat", 27.8810756))
    lon = float(request.args.get("lon", -82.7475217))
    results = {}
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, 1.0)
    envelope = f"{mn_lon},{mn_lat},{mx_lon},{mx_lat}"
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
                })
        results["total_facilities"] = len(features)
        results["rcr_status_values"] = status_values
        results["pa_facilities"] = pa_facilities
    except Exception as e:
        results["error"] = str(e)
    return jsonify(results)


@app.route("/nexus_docs", methods=["GET"])
def nexus_docs():
    """Fetch FDEP Nexus documents for a facility and return the best contamination report."""
    facility_id = request.args.get("id", "")
    if not facility_id:
        return jsonify({"error": "id parameter required"}), 400

    PRIORITY_TYPES = {
        'REMEDIAL ACTION RELATED': 12,
        'SITE ASSESSMENT RELATED': 10,
        'OPERATION AND MAINT - REMEDIAL ACTION RPT RELATED': 10,
        'MONITORING PLANS AND REPORTS RELATED': 10,
        'REMEDIAL ACTION PLAN RELATED': 12,
        'SOURCE REMOVAL RELATED': 7,
        'COMPLETION RELATED': 7,
        'LAB ANALYTICAL REPORTS': 5,
        'POTABLE WELL SURVEY - SAMPLING': 5,
        'DISCHARGE REPORTING RELATED': 4,
    }
    GOOD_SUBJECTS = [
        'ASSESSMENT REPORT','SITE ASSESSMENT REPORT','SITE ASSESSMENT',
        'CONTAMINATION ASSESSMENT','RAGR','GENERAL REMEDIAL ACTION',
        'QUARTERLY REMEDIAL','MONITORING REPORT','REMEDIAL ACTION REPORT',
        'REMEDIAL ACTION','CLOSURE REPORT','GROUNDWATER','GROUNDWATER REPORT',
        'SOIL ASSESSMENT','INTERIM ASSESSMENT','INTERIM REPORT',
        'ANNUAL REPORT','QUARTERLY REPORT',
        'SSAR','LSSAR','CAR','PARM REPORT','NATURAL ATTENUATION',
        'NAM REPORT','LETTER REPORT','SAMPLING REPORT',
        'LSRAP','LSRAR','RAIR','LSSI SAR','LSSI FINAL','FINAL DELIVERABLE',
        'SSA REPORT',
    ]
    BAD_SUBJECTS = [
        'INVOICE','RATE SHEET','HASP','CONFIRMATION','UPLOAD',
        'ZIP','NOTIFICATION','RECEIPT','CHECKLIST','EXCEL TABLES',
        'SPREADSHEET','TABLES ONLY','ACCEPTANCE LETTER','REVIEW LTR',
        'APPROVAL ORDER','NOTICE OF',' - LR','- LR ',
        'RVW LTR','REVIEW LETTER','COVER LETTER','TRANSMITTAL',
        'ACKNOWLEDGEMENT','TRANSFER MEMO',
        'SUPP INFO','RESPONSE TO COMMENTS','RTC','DRCL',
        'DEFICIENCY','COMMENT LETTER','RESUBMIT',
        'PARTIAL REPORT','FOR INVOICING','INTERIM DELIVERABLE',
        'COST PROPOSAL','PROPOSAL RELATED',
        'COMMENTS','TELECON','TELECONFERENCE NOTES','NEXT PHASE',
    ]

    def parse_date(d):
        from datetime import datetime
        try: return datetime.strptime(d.strip(), '%m-%d-%Y')
        except: return datetime.min

    def score_doc(row):
        import re
        score = PRIORITY_TYPES.get(row.get('DOCUMENT TYPE',''), 0)
        subj = row.get('SUBJECT','').upper().strip()
        for kw in GOOD_SUBJECTS:
            if kw in subj: score += 2
        for kw in BAD_SUBJECTS:
            if kw in subj: score -= 3
        if re.search(r'\bRAP\b', subj): score += 2
        if re.search(r'\bNAM\b', subj): score += 2
        if re.search(r'\bLSRAP\b|\bLSRAR\b|\bL\dLSRAP\b', subj): score += 2
        d = parse_date(row.get('DOCUMENT DATE',''))
        if d.year >= 2024: score += 3
        elif d.year >= 2022: score += 2
        elif d.year >= 2019: score += 1
        return score

    try:
        import csv, io
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/csv,*/*",
            "Referer": (f"https://prodenv.dep.state.fl.us/DepNexus/public/"
                       f"electronic-documents/{facility_id}/facility!search")
        }
        url = (f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents"
               f"/{facility_id}/export?wildCardMatch=false&page=1")
        r = requests.get(url, timeout=20, headers=headers)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        rows = list(reader)
    except Exception as e:
        return jsonify({"error": str(e), "facility_id": facility_id})

    candidates = [row for row in rows if row.get('DOCUMENT TYPE','') in PRIORITY_TYPES]
    if not candidates:
        return jsonify({
            "facility_id": facility_id,
            "total_docs": len(rows),
            "best_doc": None,
            "message": "No priority documents found — see Nexus for full list"
        })

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
    facility_id = request.args.get("id", "9045812")
    search = request.args.get("q", "5140103").upper()
    results = {}
    try:
        import csv, io
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents/{facility_id}/facility!search"
        }
        r1 = requests.get(
            f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents/{facility_id}/export?wildCardMatch=false&page=1",
            timeout=20, headers=hdrs)
        rows1 = list(csv.DictReader(io.StringIO(r1.text)))
        results["page1_count"] = len(rows1)
        results["page1_first"] = {"date": rows1[0].get("DOCUMENT DATE"), "subject": rows1[0].get("SUBJECT")} if rows1 else None
        results["page1_last"] = {"date": rows1[-1].get("DOCUMENT DATE"), "subject": rows1[-1].get("SUBJECT")} if rows1 else None
        r2 = requests.get(
            f"https://prodenv.dep.state.fl.us/DepNexus/public/electronic-documents/{facility_id}/export?wildCardMatch=false&page=2",
            timeout=20, headers=hdrs)
        rows2 = list(csv.DictReader(io.StringIO(r2.text)))
        results["page2_count"] = len(rows2)
        results["page2_first"] = {"date": rows2[0].get("DOCUMENT DATE"), "subject": rows2[0].get("SUBJECT")} if rows2 else None
        results["page1_same_as_page2"] = (rows1 == rows2)
        results["total_docs_from_page1"] = len(rows1)
    except Exception as e:
        results["error"] = str(e)
    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
