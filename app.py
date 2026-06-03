"""
Phase I ESA Database Proxy — v9
Clean rewrite. FRS/NPL/CERCLA use WHERE bbox. All other EPA use ECHO REST API.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests, math

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
        r = requests.get(url, params=params, timeout=20)
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
FL_SUPERFUND = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/1/query"
CHAZ         = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CHAZ/MapServer/5/query"
STCM_TANKS   = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/1/query"
STCM_LUST    = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/2/query"
SOLID_WASTE  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_WASTE_ICR_BACKG/MapServer/1/query"
ICR          = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_WASTE_ICR_BACKG/MapServer/12/query"

# ── EPA FRS URLs ──────────────────────────────────────────────────────────────
FRS_SEMS  = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21/query"
FRS_ACRES = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/0/query"

# ── DEP Cleanup field constants ───────────────────────────────────────────────
DEP_FIELDS  = "BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY,SOURCE_DATABASE_NAME"
SUPER_WHERE = "CLCC_CLEANUP_CATEGORY_KEY='SUPER'"
CONT_WHERE  = "CLCC_CLEANUP_CATEGORY_KEY IN ('OTHCU','PFAS')"
LUST_WHERE  = "CLCC_CLEANUP_CATEGORY_KEY='PETRO'"
BROWN_WHERE = "CLCC_CLEANUP_CATEGORY_KEY='BROWN'"
VOL_WHERE   = "SOURCE_DATABASE_NAME IN ('DRYCLEANING','RESPONSPARTY')"
DEP_NC = {"SRCO","ISSA","SSA","PA","SI","RI","FS","RD","RA","OAM",
           "OPEN","ACTIVE","INPROCESS","AWAITFUND","AWAITSITEACCESS","ELIGREVIEW"}

# ── EPA ECHO RCRA ─────────────────────────────────────────────────────────────
def echo_rcra(lat, lon, radius_miles, handler_types):
    import time
    url = "https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info"
    params = {"output":"JSON","p_lat":lat,"p_lon":lon,"p_radius_mi":radius_miles,
              "p_htype":handler_types,"qcolumns":"1,2,3,4,5,6,38,39,40"}
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2 * attempt)  # 2s, 4s backoff on retry
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))  # extra wait on rate limit
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
    return {"count": 0, "sites": [], "error": f"Failed after 3 attempts: {last_error}"}

# ── USACE FUDS ────────────────────────────────────────────────────────────────
def fuds(lat, lon, radius_miles):
    data = fdep_query(
        "https://services7.arcgis.com/n1YM8pTrFmm7L4hs/arcgis/rest/services/FUDS_Projects/FeatureServer/0/query",
        lat, lon, radius_miles, out_fields="PROJECT_NAME,PROJECT_STATUS,LATITUDE,LONGITUDE")
    sites = []
    for feat in data.get("features", []):
        attrs  = feat.get("attributes", {})
        geom   = feat.get("geometry", {})
        status = attrs.get("PROJECT_STATUS", "") or ""
        flat   = float(attrs.get("LATITUDE", 0) or geom.get("y", 0))
        flon   = float(attrs.get("LONGITUDE", 0) or geom.get("x", 0))
        dist   = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
        nc     = status.upper() not in {"CLOSED","COMPLETE","NO FURTHER ACTION","NFA"}
        sites.append({"name": attrs.get("PROJECT_NAME","Unknown FUDS"), "distance": dist, "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── FRS NPL ───────────────────────────────────────────────────────────────────
def frs_npl(lat, lon, radius_miles, status_filter=None):
    # INTEREST_TYPE values: "SUPERFUND NPL", "SUPERFUND (NON-NPL)"
    # ACTIVE_STATUS values: "NOT ON THE NPL", "DELETED FROM THE FINAL NPL", "CURRENTLY ON THE FINAL NPL" etc
    data = frs_spatial(FRS_SEMS, lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,INTEREST_TYPE,ACTIVE_STATUS,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        interest = str(attrs.get("INTEREST_TYPE","") or "")
        active   = str(attrs.get("ACTIVE_STATUS","") or "")
        # Only include NPL sites
        if interest != "SUPERFUND NPL":
            continue
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
    data = frs_spatial(FRS_SEMS, lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,INTEREST_TYPE,ACTIVE_STATUS,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = []
    for feat in data.get("features", []):
        attrs    = feat.get("attributes", {})
        interest = str(attrs.get("INTEREST_TYPE","") or "")
        active   = str(attrs.get("ACTIVE_STATUS","") or "")
        # Only non-NPL CERCLA/SEMS sites
        if interest not in {"SUPERFUND (NON-NPL)","SUPERFUND NON-NPL","CERCLA"}:
            continue
        flat = float(attrs.get("LATITUDE83", 0) or 0)
        flon = float(attrs.get("LONGITUDE83", 0) or 0)
        if not flat or not flon:
            continue
        dist = haversine(lat, lon, flat, flon)
        if dist > radius_miles:
            continue
        nc = active.upper() not in {"ARCHIVED","INACTIVE","DELETED FROM THE FINAL NPL"}
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
        sites = []
        for feat in data.get("features", []):
            attrs = feat.get("attributes", {}); geom = feat.get("geometry", {})
            name = str(attrs.get("ME_NAME") or "Unknown"); dist = 999.0
            if geom and "x" in geom and "y" in geom:
                try: dist = round(haversine(lat, lon, float(geom["y"]), float(geom["x"])), 2)
                except: pass
            gen = str(attrs.get("GENERATOR","") or ""); perm = str(attrs.get("PERMITTED_CONSENTED","") or "")
            nc = gen in {"LQG","SQG","VSQG"} or perm == "Y"
            sites.append({"name": name, "distance": dist, "status": str(attrs.get("FAC_INS_TYPE","") or ""), "nc": nc})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}

    def get_lust():
        s1 = parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=LUST_WHERE, out_fields=DEP_FIELDS),
            lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
        s2 = parse_fdep(fdep_query(STCM_LUST, lat, lon, 0.5, out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE"),
            lat, lon, "SITE_NAME", "SITE_STATUS", {"OPEN","ACTIVE","Active","Open"})
        seen = set(); out = []
        for s in sorted(s1+s2, key=lambda x: x["distance"]):
            if s["name"] not in seen: seen.add(s["name"]); out.append(s)
        return {"count": len(out), "sites": out}

    def get_brownfields():
        fdep_sites = parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS),
            lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
        epa_data = frs_spatial(FRS_ACRES, lat, lon, 0.5,
            out_fields="PRIMARY_NAME,LATITUDE83,LONGITUDE83,ACTIVE_STATUS,INTEREST_TYPE")
        # Only include confirmed brownfield properties from ACRES
        epa_sites = []
        for feat in epa_data.get("features", []):
            attrs = feat.get("attributes", {})
            interest = str(attrs.get("INTEREST_TYPE","") or "")
            if interest != "BROWNFIELDS PROPERTY":
                continue
            flat = float(attrs.get("LATITUDE83", 0) or 0)
            flon = float(attrs.get("LONGITUDE83", 0) or 0)
            if not flat or not flon:
                continue
            dist = haversine(lat, lon, flat, flon)
            if dist > 0.5:
                continue
            st = str(attrs.get("ACTIVE_STATUS","") or "").upper()
            nc = st not in {"COMPLETE","COMPLETED","DELETED","ARCHIVED","READY FOR ANTICIPATED USE"}
            epa_sites.append({"name": str(attrs.get("PRIMARY_NAME","Unknown")),
                              "distance": round(dist,2), "status": str(attrs.get("ACTIVE_STATUS","") or ""), "nc": nc})
        seen = set(); out = []
        for s in sorted(fdep_sites+epa_sites, key=lambda x: x["distance"]):
            if s["name"] not in seen: seen.add(s["name"]); out.append(s)
        return {"count": len(out), "sites": out}

    def mk(fn): return lambda p: {"count": len(p), "sites": p}

    task_map = {
        "npl":            lambda: frs_npl(lat, lon, 1.0, status_filter=["Currently on the Final NPL","Proposed for NPL"]),
        "fuds":           lambda: fuds(lat, lon, 1.0),
        "rcra_ca":        lambda: echo_rcra(lat, lon, 1.0, "CA"),
        "state_superfund":get_state_superfund,
        "npl_del":        get_delisted,
        "cercla":         lambda: cercla(lat, lon, 0.5),
        "rcra_tsd":       lambda: echo_rcra(lat, lon, 0.5, "TSD"),
        "haz":            get_haz,
        "cont":           lambda: mk(None)(parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields=DEP_FIELDS), lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)),
        "solid":          lambda: mk(None)(parse_fdep(fdep_query(SOLID_WASTE, lat, lon, 0.5, where="FACILITY_STATUS NOT IN ('Closed','Inactive','CLOSED','INACTIVE','Closed, No Gw Monitoring','Closed, Gw Monitoring')", out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE"), lat, lon, "FACILITY_NAME", "FACILITY_STATUS", {"Active","ACTIVE","Open","OPEN","Authorized To Operate","Authorized to Operate","Partially Closed","Active, No Gw Monitoring"})),
        "lust":           get_lust,
        "vol":            lambda: mk(None)(parse_fdep(fdep_query(DEP_CLEANUP, lat, lon, 0.5, where=VOL_WHERE, out_fields=DEP_FIELDS), lat, lon, "BUSINESS_NAME", "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)),
        "brown":          get_brownfields,
        "ust":            lambda: mk(None)(parse_fdep(fdep_query(STCM_TANKS, lat, lon, 0.15, out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_CLEANUP_STATUS"), lat, lon, "FACILITY_NAME", "FACILITY_STATUS", {"Active","ACTIVE","Open","OPEN"})),
        "rcra_gen":       lambda: echo_rcra(lat, lon, 0.15, "LQG,SQG,VSQG"),
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
        "stcm_tanks":     lambda: fdep_query(STCM_TANKS, lat, lon, 0.15, out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_CLEANUP_STATUS"),
        "solid":          lambda: fdep_query(SOLID_WASTE, lat, lon, 0.5, out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE"),
        "ic":             lambda: fdep_query(ICR, lat, lon, 0.05, out_fields="SITE_NAME,IC_STATUS,MECHANISM_TYPE"),
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
        "echo_rcra_gen":  lambda: echo_rcra(lat, lon, 0.15, "LQG,SQG,VSQG"),
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
    return jsonify({"status": "ok", "service": "Phase I ESA Proxy", "version": "9.0"})

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
