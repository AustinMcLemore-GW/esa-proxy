"""
Phase I ESA Database Proxy — v4
Uses verified FDEP layer 0 with correct field names from live API inspection.
Sends inSR=4326 so ArcGIS server handles the projection internally.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, math

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

@app.route('/')
def index():
    return app.send_static_file('index.html')

# ── Haversine distance (miles) ────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ── Generic ArcGIS spatial query (WGS84 in, WGS84 out) ───────────────────────

def arcgis_query(url, lat, lon, radius_miles, where="1=1", out_fields="*"):
    """
    Spatial query against any ArcGIS MapServer/FeatureServer layer.
    Sends coordinates in WGS84 (inSR=4326); server reprojects internally.
    """
    radius_m = radius_miles * 1609.34
    params = {
        "geometry":     f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "spatialRel":   "esriSpatialRelIntersects",
        "distance":     radius_m,
        "units":        "esriSRUnit_Meter",
        "inSR":         "4326",
        "outSR":        "4326",
        "where":        where,
        "outFields":    out_fields,
        "returnGeometry": "true",
        "f":            "json",
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

# ── Verified FDEP service URLs ────────────────────────────────────────────────

# DEP Cleanup Sites (layer 0) — the unified cleanup database
# Fields: BUSINESS_NAME, RSC2_REMEDIATION_STATUS_KEY, CLCC_CLEANUP_CATEGORY_KEY, SOURCE_DATABASE_NAME
DEP_CLEANUP = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/0/query"

# CHAZ (layer 0) — all hazardous waste facilities
# Fields: FAC_NAME, FAC_STATUS, FAC_INS_TYPE
CHAZ_ALL = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CHAZ/MapServer/0/query"

# STCM — registered tanks (layer 1) and PCTS discharges/leaking (layer 2)
STCM_TANKS = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/1/query"
STCM_LUST  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/2/query"

# Solid Waste
SOLID_WASTE = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/SolidWaste_SP/MapServer/0/query"

# ICR — institutional controls (from DWM_WASTE_ICR_BACKG layer 12)
ICR = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_WASTE_ICR_BACKG/MapServer/12/query"

# DEP Cleanup RSC2 status codes that mean NOT complete → trigger NC bullet
# RSC2_REMEDIATION_STATUS_KEY values (from DEP Cleanup Sites layer 0)
DEP_NC_STATUSES = {
    "SRCO",      # Site Rehabilitation Completion Order not yet issued
    "ISSA",      # Initial Site Assessment
    "SSA",       # Site Status Assessment
    "PA",        # Preliminary Assessment
    "SI",        # Site Investigation
    "RI",        # Remedial Investigation
    "FS",        # Feasibility Study
    "RD",        # Remedial Design
    "RA",        # Remedial Action
    "OAM",       # Operation, Maintenance & Monitoring
    "OPEN",      # Open
    "ACTIVE",
    "INPROCESS",
    "AWAITFUND",
    "AWAITSITEACCESS",
    "ELIGREVIEW",
}

# ── EPA ECHO RCRA ─────────────────────────────────────────────────────────────

def echo_rcra(lat, lon, radius_miles, handler_types):
    params = {
        "output": "JSON",
        "p_lat": lat, "p_lon": lon,
        "p_radius_mi": radius_miles,
        "p_htype": handler_types,
        "qcolumns": "1,2,3,4,5,6,38,39,40",
    }
    try:
        r = requests.get("https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info",
                         params=params, timeout=15)
        r.raise_for_status()
        facilities = r.json().get("Results", {}).get("Facilities", [])
        sites = []
        for f in facilities:
            name = f.get("FacName", "Unknown")
            flat = float(f.get("FacLat",  0) or 0)
            flon = float(f.get("FacLong", 0) or 0)
            dist = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
            status = f.get("RCRAComplianceStatus", "") or ""
            nc = "CA" in handler_types and status not in ["No Violation Identified", ""]
            sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── EPA FRS NPL ───────────────────────────────────────────────────────────────

def frs_npl(lat, lon, radius_miles, status_filter=None):
    data = arcgis_query(
        "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21/query",
        lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,NPL_STATUS_NAME,LATITUDE83,LONGITUDE83"
    )
    sites = []
    for feat in data.get("features", []):
        attrs = feat.get("attributes", {})
        geom  = feat.get("geometry", {})
        status = attrs.get("NPL_STATUS_NAME", "") or ""
        if status_filter and status not in status_filter:
            continue
        name = attrs.get("PRIMARY_NAME", "Unknown") or "Unknown"
        flat = float(attrs.get("LATITUDE83",  0) or geom.get("y", 0))
        flon = float(attrs.get("LONGITUDE83", 0) or geom.get("x", 0))
        dist = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
        nc   = status in ["Currently on the Final NPL", "Proposed for NPL"]
        sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── USACE FUDS ────────────────────────────────────────────────────────────────

def fuds(lat, lon, radius_miles):
    data = arcgis_query(
        "https://services7.arcgis.com/n1YM8pTrFmm7L4hs/arcgis/rest/services/FUDS_Projects/FeatureServer/0/query",
        lat, lon, radius_miles,
        out_fields="PROJECT_NAME,PROJECT_STATUS,LATITUDE,LONGITUDE"
    )
    sites = []
    for feat in data.get("features", []):
        attrs  = feat.get("attributes", {})
        geom   = feat.get("geometry", {})
        name   = attrs.get("PROJECT_NAME", "Unknown FUDS") or "Unknown FUDS"
        status = attrs.get("PROJECT_STATUS", "") or ""
        flat   = float(attrs.get("LATITUDE",  0) or geom.get("y", 0))
        flon   = float(attrs.get("LONGITUDE", 0) or geom.get("x", 0))
        dist   = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
        nc     = status.upper() not in {"CLOSED", "COMPLETE", "NO FURTHER ACTION", "NFA"}
        sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── CERCLA (FRS non-NPL) ──────────────────────────────────────────────────────

def cercla(lat, lon, radius_miles):
    # FRS CERCLA non-NPL sites via EPA ArcGIS Hub
    data = arcgis_query(
        "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/22/query",
        lat, lon, radius_miles,
        out_fields="PRIMARY_NAME,ACTIVE_STATUS,LATITUDE83,LONGITUDE83"
    )
    if "error" in data:
        return {"count": 0, "sites": [], "error": data["error"]}
    sites = []
    for feat in data.get("features", []):
        attrs  = feat.get("attributes", {})
        geom   = feat.get("geometry", {})
        name   = attrs.get("PRIMARY_NAME", "Unknown") or "Unknown"
        status = attrs.get("ACTIVE_STATUS", "") or ""
        flat   = float(attrs.get("LATITUDE83",  0) or geom.get("y", 0))
        flon   = float(attrs.get("LONGITUDE83", 0) or geom.get("x", 0))
        dist   = round(haversine(lat, lon, flat, flon), 2) if flat else 999.0
        nc     = status.upper() not in {"ARCHIVED", "INACTIVE", "NFRAP", "DELETED"}
        sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return {"count": len(sites), "sites": sites}

# ── ERNS ──────────────────────────────────────────────────────────────────────

def erns(zipcode):
    if not zipcode:
        return {"count": 0, "sites": [], "note": "ZIP not provided — skipped"}
    try:
        url = f"https://data.epa.gov/efservice/ERNS_INCIDENTS/ZIP_CODE/{zipcode}/rows/0:100/JSON"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        sites = [{"name": rec.get("FACILITY_NAME") or rec.get("COMPANY_NAME") or "ERNS Incident",
                  "distance": 0.0,
                  "status":   rec.get("INCIDENT_TYPE_DESCRIPTION", "") or "",
                  "nc": True}
                 for rec in data]
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

    # ── 1-mile ────────────────────────────────────────────────────────────────
    res["npl"]     = frs_npl(lat, lon, 1.0,
                         status_filter=["Currently on the Final NPL", "Proposed for NPL"])
    res["fuds"]    = fuds(lat, lon, 1.0)
    res["rcra_ca"] = echo_rcra(lat, lon, 1.0, "CA")

    # ── 0.5-mile ──────────────────────────────────────────────────────────────
    delisted = frs_npl(lat, lon, 0.5, status_filter=["Deleted from the Final NPL"])
    for s in delisted.get("sites", []):
        s["nc"] = False
    res["npl_del"] = delisted

    res["cercla"]   = cercla(lat, lon, 0.5)
    res["rcra_tsd"] = echo_rcra(lat, lon, 0.5, "TSD")

    # CHAZ — all hazardous waste facilities (layer 0)
    chaz_data  = arcgis_query(CHAZ_ALL, lat, lon, 0.5,
                     out_fields="FAC_NAME,FAC_STATUS,FAC_INS_TYPE")
    chaz_sites = parse_features(chaz_data, lat, lon, "FAC_NAME",
                     status_field="FAC_STATUS", nc_statuses={"ACTIVE", "Active"})
    res["haz"] = {"count": len(chaz_sites), "sites": chaz_sites}

    # DEP Cleanup Sites layer 0 — contamination (excl brownfields, petroleum, drycleaning)
    cont_data  = arcgis_query(DEP_CLEANUP, lat, lon, 0.5,
                     where="CLCC_CLEANUP_CATEGORY_KEY NOT IN ('BROWN','PETRO')",
                     out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY,SOURCE_DATABASE_NAME")
    cont_sites = parse_features(cont_data, lat, lon, "BUSINESS_NAME",
                     status_field="RSC2_REMEDIATION_STATUS_KEY",
                     nc_statuses=DEP_NC_STATUSES)
    res["cont"] = {"count": len(cont_sites), "sites": cont_sites}

    # Solid Waste
    solid_data  = arcgis_query(SOLID_WASTE, lat, lon, 0.5,
                      out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_TYPE")
    solid_sites = parse_features(solid_data, lat, lon, "FACILITY_NAME",
                      status_field="FACILITY_STATUS",
                      nc_statuses={"ACTIVE", "OPEN", "Active", "Open"})
    res["solid"] = {"count": len(solid_sites), "sites": solid_sites}

    # Leaking USTs — PCTS discharges layer 2
    lust_data  = arcgis_query(STCM_LUST, lat, lon, 0.5,
                     out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE")
    lust_sites = parse_features(lust_data, lat, lon, "SITE_NAME",
                     status_field="SITE_STATUS",
                     nc_statuses={"OPEN", "ACTIVE", "Active", "Open"})
    res["lust"] = {"count": len(lust_sites), "sites": lust_sites}

    # Voluntary cleanup — petroleum + drycleaning from DEP Cleanup layer 0
    vol_data  = arcgis_query(DEP_CLEANUP, lat, lon, 0.5,
                    where="CLCC_CLEANUP_CATEGORY_KEY IN ('PETRO') OR SOURCE_DATABASE_NAME LIKE '%DRYCLEANING%'",
                    out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY")
    vol_sites = parse_features(vol_data, lat, lon, "BUSINESS_NAME",
                    status_field="RSC2_REMEDIATION_STATUS_KEY",
                    nc_statuses=DEP_NC_STATUSES)
    res["vol"] = {"count": len(vol_sites), "sites": vol_sites}

    # Brownfields — from DEP Cleanup layer 0
    brown_data  = arcgis_query(DEP_CLEANUP, lat, lon, 0.5,
                      where="CLCC_CLEANUP_CATEGORY_KEY='BROWN'",
                      out_fields="BUSINESS_NAME,RSC2_REMEDIATION_STATUS_KEY,CLCC_CLEANUP_CATEGORY_KEY")
    brown_sites = parse_features(brown_data, lat, lon, "BUSINESS_NAME",
                      status_field="RSC2_REMEDIATION_STATUS_KEY",
                      nc_statuses=DEP_NC_STATUSES)
    res["brown"] = {"count": len(brown_sites), "sites": brown_sites}

    # ── Adjoining (~0.15 mi) ──────────────────────────────────────────────────
    ust_data  = arcgis_query(STCM_TANKS, lat, lon, 0.15,
                    out_fields="SITE_NAME,SITE_STATUS,TANK_COUNT")
    ust_sites = parse_features(ust_data, lat, lon, "SITE_NAME",
                    status_field="SITE_STATUS",
                    nc_statuses={"OPEN", "ACTIVE", "Active", "Open"})
    res["ust"] = {"count": len(ust_sites), "sites": ust_sites}

    res["rcra_gen"] = echo_rcra(lat, lon, 0.15, "LQG,SQG,VSQG")

    # ── Property only (~0.05 mi) ──────────────────────────────────────────────
    ic_data  = arcgis_query(ICR, lat, lon, 0.05,
                   out_fields="SITE_NAME,IC_STATUS,MECHANISM_TYPE")
    ic_sites = parse_features(ic_data, lat, lon, "SITE_NAME",
                   status_field="IC_STATUS",
                   nc_statuses={"ACTIVE", "Active"})
    res["ic"] = {"count": len(ic_sites), "sites": ic_sites}

    res["erns"] = erns(zipcode)

    return jsonify(res)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Phase I ESA Proxy", "version": "4.0"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
