"""
Phase I ESA Database Proxy — v7
All FDEP layer URLs and field names verified from live API inspection.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, math

app = Flask(__name__, static_folder='.', static_url_path='')
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

def arcgis_query(url, lat, lon, radius_miles, where="1=1", out_fields="*"):
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
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "features": []}

def parse_features(data, lat, lon, name_field, status_field=None, nc_statuses=None):
    sites = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        name  = str(attrs.get(name_field) or "Unknown")
        dist  = 999.0
        if geom and "x" in geom and "y" in geom:
            try:
                dist = round(haversine(lat, lon, float(geom["y"]), float(geom["x"])), 2)
            except Exception:
                pass
        status = str(attrs.get(status_field, "") or "") if status_field else ""
        nc = bool(nc_statuses and status in nc_statuses)
        sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return sites

# ── Verified FDEP service URLs and layer numbers ──────────────────────────────
# DEP Cleanup Sites — layer 0
# Fields: BUSINESS_NAME, RSC2_REMEDIATION_STATUS_KEY, CLCC_CLEANUP_CATEGORY_KEY, SOURCE_DATABASE_NAME
DEP_CLEANUP  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/0/query"
# Florida Superfund Waste Cleanup Sites — dedicated layer 1
FL_SUPERFUND = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/1/query"

# CHAZ — layer 5 (Compliance & Enforcement Tracking - all active facilities)
# Fields: ME_NAME, HANDLER_ID, FAC_INS_TYPE, GENERATOR, PERMITTED_CONSENTED
CHAZ = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CHAZ/MapServer/5/query"

# STCM — layer 1 (Registered Tanks), layer 2 (PCTS Discharges / Leaking)
# Layer 1 fields: FACILITY_NAME, FACILITY_STATUS, FACILITY_CLEANUP_STATUS
# Layer 2 fields: SITE_NAME, SITE_STATUS
STCM_TANKS = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/1/query"
STCM_LUST  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/2/query"

# Solid Waste Facilities — DWM_WASTE_ICR_BACKG layer 1
# Fields: FACILITY_NAME, FACILITY_STATUS, CLASS, FACILITY_TYPE
SOLID_WASTE = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_WASTE_ICR_BACKG/MapServer/1/query"

# ICR — DWM_WASTE_ICR_BACKG layer 12
ICR = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_WASTE_ICR_BACKG/MapServer/12/query"

# DEP Cleanup category mappings (CLCC_CLEANUP_CATEGORY_KEY):
# PETRO  → LUST
# BROWN  → Brownfields
# SUPER/OTHCU/PFAS → Contamination sites
# SOURCE_DATABASE_NAME DRYCLEANING/RESPONSPARTY → Voluntary cleanup
DEP_FIELDS  = "BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY,SOURCE_DATABASE_NAME"
# State Superfund equivalent = SUPER + OTHCU at 1 mile (separate category)
SUPER_WHERE = "CLCC_CLEANUP_CATEGORY_KEY IN ('SUPER','OTHCU')"
# Contamination = PFAS only now (SUPER/OTHCU moved to state_superfund)
CONT_WHERE  = "CLCC_CLEANUP_CATEGORY_KEY IN ('PFAS','OTHCU') AND PROGRAM_TYPE NOT IN ('CERCLA','NPL','FEDERAL','STATE')"
LUST_WHERE  = "CLCC_CLEANUP_CATEGORY_KEY='PETRO'"
BROWN_WHERE = "CLCC_CLEANUP_CATEGORY_KEY='BROWN'"
VOL_WHERE   = "SOURCE_DATABASE_NAME IN ('DRYCLEANING','RESPONSPARTY')"

# RSC2 status values = not complete → triggers NC bullet
DEP_NC = {
    "SRCO","ISSA","SSA","PA","SI","RI","FS","RD","RA","OAM",
    "OPEN","ACTIVE","INPROCESS","AWAITFUND","AWAITSITEACCESS","ELIGREVIEW",
}

# ── EPA ECHO RCRA ─────────────────────────────────────────────────────────────

def echo_rcra(lat, lon, radius_miles, handler_types):
    try:
        r = requests.get(
            "https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info",
            params={"output":"JSON","p_lat":lat,"p_lon":lon,
                    "p_radius_mi":radius_miles,"p_htype":handler_types,
                    "qcolumns":"1,2,3,4,5,6,38,39,40"}, timeout=15)
        r.raise_for_status()
        facilities = r.json().get("Results", {}).get("Facilities", [])
        sites = []
        for f in facilities:
            flat = float(f.get("FacLat",  0) or 0)
            flon = float(f.get("FacLong", 0) or 0)
            dist = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
            status = f.get("RCRAComplianceStatus", "") or ""
            nc = "CA" in handler_types and status not in ["No Violation Identified",""]
            sites.append({"name":f.get("FacName","Unknown"),"distance":dist,"status":status,"nc":nc})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── EPA FRS NPL ───────────────────────────────────────────────────────────────

def frs_npl(lat, lon, radius_miles, status_filter=None):
    data = arcgis_query(
        "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21/query",
        lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,NPL_STATUS_NAME,LATITUDE83,LONGITUDE83")
    sites = []
    for feat in data.get("features", []):
        attrs  = feat.get("attributes", {})
        geom   = feat.get("geometry", {})
        status = attrs.get("NPL_STATUS_NAME", "") or ""
        if status_filter and status not in status_filter:
            continue
        flat = float(attrs.get("LATITUDE83",  0) or geom.get("y", 0))
        flon = float(attrs.get("LONGITUDE83", 0) or geom.get("x", 0))
        dist = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
        nc   = status in ["Currently on the Final NPL","Proposed for NPL"]
        sites.append({"name":attrs.get("PRIMARY_NAME","Unknown"),"distance":dist,"status":status,"nc":nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── USACE FUDS ────────────────────────────────────────────────────────────────

def fuds(lat, lon, radius_miles):
    data = arcgis_query(
        "https://services7.arcgis.com/n1YM8pTrFmm7L4hs/arcgis/rest/services/FUDS_Projects/FeatureServer/0/query",
        lat, lon, radius_miles,
        out_fields="PROJECT_NAME,PROJECT_STATUS,LATITUDE,LONGITUDE")
    sites = []
    for feat in data.get("features", []):
        attrs  = feat.get("attributes", {})
        geom   = feat.get("geometry", {})
        status = attrs.get("PROJECT_STATUS","") or ""
        flat   = float(attrs.get("LATITUDE",  0) or geom.get("y", 0))
        flon   = float(attrs.get("LONGITUDE", 0) or geom.get("x", 0))
        dist   = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
        nc     = status.upper() not in {"CLOSED","COMPLETE","NO FURTHER ACTION","NFA"}
        sites.append({"name":attrs.get("PROJECT_NAME","Unknown FUDS"),"distance":dist,"status":status,"nc":nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── CERCLA ────────────────────────────────────────────────────────────────────

def cercla(lat, lon, radius_miles):
    data = arcgis_query(
        "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/22/query",
        lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,ACTIVE_STATUS,LATITUDE83,LONGITUDE83")
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = []
    for feat in data.get("features", []):
        attrs  = feat.get("attributes", {})
        geom   = feat.get("geometry", {})
        status = attrs.get("ACTIVE_STATUS","") or ""
        flat   = float(attrs.get("LATITUDE83",  0) or geom.get("y", 0))
        flon   = float(attrs.get("LONGITUDE83", 0) or geom.get("x", 0))
        dist   = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
        nc     = status.upper() not in {"ARCHIVED","INACTIVE","NFRAP","DELETED"}
        sites.append({"name":attrs.get("PRIMARY_NAME","Unknown"),"distance":dist,"status":status,"nc":nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── ERNS ──────────────────────────────────────────────────────────────────────

def erns(zipcode):
    if not zipcode:
        return {"count": 0, "sites": [], "note": "ZIP not provided"}
    try:
        r = requests.get(
            f"https://data.epa.gov/efservice/ERNS_INCIDENTS/ZIP_CODE/{zipcode}/rows/0:100/JSON",
            timeout=15)
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
    zipcode = request.args.get("zip", "")
    res = {}

    # 1-mile
    res["npl"]     = frs_npl(lat, lon, 1.0,
                         status_filter=["Currently on the Final NPL","Proposed for NPL"])
    res["fuds"]    = fuds(lat, lon, 1.0)
    res["rcra_ca"] = echo_rcra(lat, lon, 1.0, "CA")

    # State Superfund equivalent — 1 mile
    # Query both: dedicated FL Superfund layer (MapServer/1) + DEP Cleanup SUPER/OTHCU (MapServer/0)
    sup_layer1 = arcgis_query(FL_SUPERFUND, lat, lon, 1.0,
                     out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY")
    sup_sites1 = parse_features(sup_layer1, lat, lon, "BUSINESS_NAME",
                     "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
    sup_layer0 = arcgis_query(DEP_CLEANUP, lat, lon, 1.0, where=SUPER_WHERE, out_fields=DEP_FIELDS)
    sup_sites0 = parse_features(sup_layer0, lat, lon, "BUSINESS_NAME",
                     "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
    seen_sup = set()
    sup_deduped = []
    for s in sorted(sup_sites1 + sup_sites0, key=lambda x: x["distance"]):
        if s["name"] not in seen_sup:
            seen_sup.add(s["name"]); sup_deduped.append(s)
    res["state_superfund"] = {"count": len(sup_deduped), "sites": sup_deduped}

    # 0.5-mile
    delisted = frs_npl(lat, lon, 0.5, status_filter=["Deleted from the Final NPL"])
    for s in delisted.get("sites", []): s["nc"] = False
    res["npl_del"] = delisted
    res["cercla"]   = cercla(lat, lon, 0.5)
    res["rcra_tsd"] = echo_rcra(lat, lon, 0.5, "TSD")

    # CHAZ layer 5 — all hazardous waste facilities, NC only if active generator
    chaz_data  = arcgis_query(CHAZ, lat, lon, 0.5,
                     out_fields="ME_NAME,FAC_INS_TYPE,GENERATOR,PERMITTED_CONSENTED")
    chaz_sites = []
    for feat in chaz_data.get("features", []):
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        name  = str(attrs.get("ME_NAME") or "Unknown")
        dist  = 999.0
        if geom and "x" in geom and "y" in geom:
            try:
                dist = round(haversine(lat, lon, float(geom["y"]), float(geom["x"])), 2)
            except Exception:
                pass
        gen    = str(attrs.get("GENERATOR","") or "")
        perm   = str(attrs.get("PERMITTED_CONSENTED","") or "")
        status = str(attrs.get("FAC_INS_TYPE","") or "")
        nc     = gen in {"LQG","SQG","VSQG"} or perm == "Y"
        chaz_sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
    chaz_sites.sort(key=lambda s: s["distance"])
    res["haz"] = {"count": len(chaz_sites), "sites": chaz_sites}

    # Contamination = SUPER, OTHCU, PFAS
    cont_data  = arcgis_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields=DEP_FIELDS)
    cont_sites = parse_features(cont_data, lat, lon, "BUSINESS_NAME",
                     "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
    res["cont"] = {"count": len(cont_sites), "sites": cont_sites}

    # Solid Waste — DWM_WASTE_ICR_BACKG layer 1 (FACILITY_NAME, FACILITY_STATUS)
    solid_data  = arcgis_query(SOLID_WASTE, lat, lon, 0.5,
                      out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE")
    solid_sites = parse_features(solid_data, lat, lon, "FACILITY_NAME",
                      "FACILITY_STATUS", {"Active","ACTIVE"})
    res["solid"] = {"count": len(solid_sites), "sites": solid_sites}

    # LUST = PETRO from DEP Cleanup + STCM layer 2 (PCTS discharges)
    lust_dep   = arcgis_query(DEP_CLEANUP, lat, lon, 0.5, where=LUST_WHERE, out_fields=DEP_FIELDS)
    lust_sites = parse_features(lust_dep, lat, lon, "BUSINESS_NAME",
                     "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
    lust_stcm  = arcgis_query(STCM_LUST, lat, lon, 0.5,
                     out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE")
    lust_stcm_sites = parse_features(lust_stcm, lat, lon, "SITE_NAME",
                          "SITE_STATUS", {"OPEN","ACTIVE","Active","Open"})
    seen = set()
    deduped = []
    for s in sorted(lust_sites + lust_stcm_sites, key=lambda x: x["distance"]):
        if s["name"] not in seen:
            seen.add(s["name"]); deduped.append(s)
    res["lust"] = {"count": len(deduped), "sites": deduped}

    # Voluntary cleanup = DRYCLEANING + RESPONSPARTY
    vol_data  = arcgis_query(DEP_CLEANUP, lat, lon, 0.5, where=VOL_WHERE, out_fields=DEP_FIELDS)
    vol_sites = parse_features(vol_data, lat, lon, "BUSINESS_NAME",
                    "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
    res["vol"] = {"count": len(vol_sites), "sites": vol_sites}

    # Brownfields = BROWN
    brown_data  = arcgis_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS)
    brown_sites = parse_features(brown_data, lat, lon, "BUSINESS_NAME",
                      "RSC2_REMEDIATION_STATUS_KEY", DEP_NC)
    res["brown"] = {"count": len(brown_sites), "sites": brown_sites}

    # Adjoining — STCM layer 1 (registered tanks): FACILITY_NAME, FACILITY_STATUS
    ust_data  = arcgis_query(STCM_TANKS, lat, lon, 0.15,
                    out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_CLEANUP_STATUS")
    ust_sites = parse_features(ust_data, lat, lon, "FACILITY_NAME",
                    "FACILITY_STATUS", {"Active","ACTIVE","Open","OPEN"})
    res["ust"] = {"count": len(ust_sites), "sites": ust_sites}
    res["rcra_gen"] = echo_rcra(lat, lon, 0.15, "LQG,SQG,VSQG")

    # Property only — ICR layer 12
    ic_data  = arcgis_query(ICR, lat, lon, 0.05,
                   out_fields="SITE_NAME,IC_STATUS,MECHANISM_TYPE")
    ic_sites = parse_features(ic_data, lat, lon, "SITE_NAME",
                   "IC_STATUS", {"ACTIVE","Active"})
    res["ic"] = {"count": len(ic_sites), "sites": ic_sites}

    res["erns"] = erns(zipcode)
    return jsonify(res)

# ── Debug endpoint ────────────────────────────────────────────────────────────

@app.route("/debug", methods=["GET"])
def debug():
    db = request.args.get("db","dep_cont")
    try:
        lat = float(request.args.get("lat", 27.852924))
        lon = float(request.args.get("lon", -82.703508))
    except ValueError:
        return jsonify({"error": "bad lat/lon"}), 400

    routes = {
        "chaz":         lambda: arcgis_query(CHAZ, lat, lon, 0.5, out_fields="ME_NAME,FAC_INS_TYPE,GENERATOR,PERMITTED_CONSENTED"),
        "stcm_lust":    lambda: arcgis_query(STCM_LUST, lat, lon, 0.5, out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE"),
        "stcm_tanks":   lambda: arcgis_query(STCM_TANKS, lat, lon, 0.15, out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_CLEANUP_STATUS"),
        "solid":        lambda: arcgis_query(SOLID_WASTE, lat, lon, 0.5, out_fields="FACILITY_NAME,FACILITY_STATUS,CLASS,FACILITY_TYPE"),
        "ic":           lambda: arcgis_query(ICR, lat, lon, 0.05, out_fields="SITE_NAME,IC_STATUS,MECHANISM_TYPE"),
        "dep_cont":     lambda: arcgis_query(DEP_CLEANUP, lat, lon, 0.5, where=CONT_WHERE, out_fields=DEP_FIELDS),
        "dep_lust":     lambda: arcgis_query(DEP_CLEANUP, lat, lon, 0.5, where=LUST_WHERE, out_fields=DEP_FIELDS),
        "dep_vol":      lambda: arcgis_query(DEP_CLEANUP, lat, lon, 0.5, where=VOL_WHERE, out_fields=DEP_FIELDS),
        "dep_brown":    lambda: arcgis_query(DEP_CLEANUP, lat, lon, 0.5, where=BROWN_WHERE, out_fields=DEP_FIELDS),
        "state_superfund": lambda: arcgis_query(FL_SUPERFUND, lat, lon, 1.0, out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY"),
        "echo_rcra_ca": lambda: echo_rcra(lat, lon, 1.0, "CA"),
        "echo_rcra_tsd":lambda: echo_rcra(lat, lon, 0.5, "TSD"),
        "echo_rcra_gen":lambda: echo_rcra(lat, lon, 0.15, "LQG,SQG,VSQG"),
        "frs_npl":      lambda: frs_npl(lat, lon, 1.0),
        "fuds":         lambda: fuds(lat, lon, 1.0),
        "cercla":       lambda: cercla(lat, lon, 0.5),
    }
    if db not in routes:
        return jsonify({"error": f"unknown db '{db}'", "options": list(routes.keys())}), 400
    return jsonify(routes[db]())

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"ok","service":"Phase I ESA Proxy","version":"6.0"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
