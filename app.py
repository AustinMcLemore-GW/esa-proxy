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
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "features": []}

def frs_query(url, where, out_fields):
    """EPA FRS ArcGIS layers — use plain WHERE clause (no spatial params, geographic CRS)."""
    try:
        r = requests.get(url, params={
            "where": where, "outFields": out_fields,
            "returnGeometry": "false", "f": "json"
        }, timeout=30)
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
SUPER_WHERE = "CLCC_CLEANUP_CATEGORY_KEY IN ('SUPER','OTHCU')"
CONT_WHERE  = "CLCC_CLEANUP_CATEGORY_KEY='PFAS'"
LUST_WHERE  = "CLCC_CLEANUP_CATEGORY_KEY='PETRO'"
BROWN_WHERE = "CLCC_CLEANUP_CATEGORY_KEY='BROWN'"
VOL_WHERE   = "SOURCE_DATABASE_NAME IN ('DRYCLEANING','RESPONSPARTY')"
DEP_NC = {"SRCO","ISSA","SSA","PA","SI","RI","FS","RD","RA","OAM",
           "OPEN","ACTIVE","INPROCESS","AWAITFUND","AWAITSITEACCESS","ELIGREVIEW"}

# ── EPA ECHO RCRA ─────────────────────────────────────────────────────────────
def echo_rcra(lat, lon, radius_miles, handler_types):
    try:
        r = requests.get("https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info",
            params={"output":"JSON","p_lat":lat,"p_lon":lon,"p_radius_mi":radius_miles,
                    "p_htype":handler_types,"qcolumns":"1,2,3,4,5,6,38,39,40"}, timeout=30)
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
        return {"count": 0, "sites": [], "error": str(e)}

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
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, radius_miles)
    where = f"LATITUDE83>={mn_lat} AND LATITUDE83<={mx_lat} AND LONGITUDE83>={mn_lon} AND LONGITUDE83<={mx_lon}"
    data = frs_query(FRS_SEMS, where, "PRIMARY_NAME,NPL_STATUS_NAME,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = parse_frs(data, lat, lon, "PRIMARY_NAME", "LATITUDE83", "LONGITUDE83", radius_miles,
                      status_field="NPL_STATUS_NAME",
                      nc_statuses={"Currently on the Final NPL","Proposed for NPL"})
    if status_filter:
        sites = [s for s in sites if s["status"] in status_filter]
    return {"count": len(sites), "sites": sites}

# ── CERCLA ────────────────────────────────────────────────────────────────────
def cercla(lat, lon, radius_miles):
    mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, radius_miles)
    where = f"LATITUDE83>={mn_lat} AND LATITUDE83<={mx_lat} AND LONGITUDE83>={mn_lon} AND LONGITUDE83<={mx_lon}"
    data = frs_query(FRS_SEMS, where, "PRIMARY_NAME,NPL_STATUS_NAME,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = parse_frs(data, lat, lon, "PRIMARY_NAME", "LATITUDE83", "LONGITUDE83", radius_miles,
                      status_field="NPL_STATUS_NAME",
                      nc_statuses={"Currently on the Final NPL","Proposed for NPL"},
                      exclude_statuses={"Currently on the Final NPL","Proposed for NPL"})
    return {"count": len(sites), "sites": sites}

# ── ERNS ──────────────────────────────────────────────────────────────────────
def erns(zipcode):
    if not zipcode:
        return {"count": 0, "sites": [], "note": "ZIP not provided"}
    try:
        r = requests.get(f"https://data.epa.gov/efservice/ERNS_INCIDENTS/ZIP_CODE/{zipcode}/rows/0:100/JSON", timeout=30)
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
        r = frs_npl(lat, lon, 0.5, status_filter=["Deleted from the Final NPL"])
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
        mn_lat, mx_lat, mn_lon, mx_lon = bbox(lat, lon, 0.5)
        where_b = f"LATITUDE83>={mn_lat} AND LATITUDE83<={mx_lat} AND LONGITUDE83>={mn_lon} AND LONGITUDE83<={mx_lon}"
        epa_data = frs_query(FRS_ACRES, where_b, "PRIMARY_NAME,LATITUDE83,LONGITUDE83,SITE_STATUS")
        epa_sites = parse_frs(epa_data, lat, lon, "PRIMARY_NAME", "LATITUDE83", "LONGITUDE83", 0.5,
            status_field="SITE_STATUS",
            nc_statuses=None)
        for s in epa_sites:
            s["nc"] = s["status"].upper() not in {"COMPLETE","COMPLETED","DELETED","ARCHIVED"}
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

    with ThreadPoolExecutor(max_workers=16) as executor:
        future_to_key = {executor.submit(fn): key for key, fn in task_map.items()}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                res[key] = future.result()
            except Exception as e:
                res[key] = {"count": 0, "sites": [], "error": str(e)}

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
        r = requests.get(url, params={"where":"1=1","resultRecordCount":"1","f":"json"}, timeout=30)
        results["test1_minimal"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["test1_minimal"] = {"error": str(e)}
    # Test 2: count only
    try:
        r = requests.get(url, params={"where":"1=1","returnCountOnly":"true","f":"json"}, timeout=30)
        results["test2_count"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["test2_count"] = {"error": str(e)}
    # Test 3: get all field names from layer
    try:
        r = requests.get("https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21",
            params={"f":"json"}, timeout=30)
        data = r.json()
        fields = [f["name"] for f in data.get("fields", [])]
        results["test3_fields"] = {"status": r.status_code, "fields": fields}
    except Exception as e:
        results["test3_fields"] = {"error": str(e)}
    # Test 4: spatial query using geometry (the correct approach for this layer)
    try:
        r = requests.get(url, params={
            "geometry": "-82.7,-82.6,27.7,27.8",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": "4269",
            "outSR": "4269",
            "outFields": "*",
            "resultRecordCount": "3",
            "returnGeometry": "false",
            "f": "json"
        }, timeout=30)
        results["test4_envelope"] = {"status": r.status_code, "body": r.json()}
    except Exception as e:
        results["test4_envelope"] = {"error": str(e)}
    return jsonify(results)

# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Phase I ESA Proxy", "version": "9.0"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
