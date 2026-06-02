"""
Phase I ESA Database Proxy
Queries all ASTM E1527-21 required databases and returns results with CORS headers.
Deploy on Render (free tier) — see README.md for instructions.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import math

app = Flask(__name__)
CORS(app)  # Allow all origins — required for browser artifact to call this

# ── Haversine distance (miles) ────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# ── FDEP ArcGIS spatial query helper ─────────────────────────────────────────

def fdep_query(service_url, lat, lon, radius_miles, where_clause="1=1", out_fields="*"):
    """Query an FDEP ArcGIS Feature Service layer by lat/lon radius."""
    radius_meters = radius_miles * 1609.34
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_meters,
        "inSR": "4326",
        "outSR": "4326",
        "where": where_clause,
        "outFields": out_fields,
        "returnGeometry": "true",
        "f": "json"
    }
    try:
        r = requests.get(service_url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e), "features": []}

def extract_fdep_sites(data, lat, lon, name_field, status_field=None, nc_statuses=None):
    """Parse FDEP ArcGIS response into standardized site list."""
    sites = []
    for feature in data.get("features", []):
        attrs = feature.get("attributes", {})
        geom = feature.get("geometry", {})
        name = attrs.get(name_field, "Unknown Site") or "Unknown Site"
        dist = 999.0
        if geom and "x" in geom and "y" in geom:
            dist = round(haversine(lat, lon, geom["y"], geom["x"]), 2)
        status = attrs.get(status_field, "") if status_field else ""
        nc = False
        if nc_statuses and status in nc_statuses:
            nc = True
        sites.append({"name": str(name), "distance": dist, "status": str(status), "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return sites

# ── EPA ECHO RCRA helper ───────────────────────────────────────────────────────

def echo_rcra_query(lat, lon, radius_miles, handler_types):
    """
    Query EPA ECHO RCRA facilities by lat/lon radius and handler type.
    handler_types: comma-separated string e.g. "CA", "TSD", "LQG,SQG,VSQG"
    """
    url = "https://echodata.epa.gov/echo/rcra_rest_services.get_facility_info"
    params = {
        "output": "JSON",
        "p_lat": lat,
        "p_lon": lon,
        "p_radius_mi": radius_miles,
        "p_htype": handler_types,
        "qcolumns": "1,2,3,4,5,6,38,39,40"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        facilities = data.get("Results", {}).get("Facilities", [])
        sites = []
        for f in facilities:
            name = f.get("FacName", "Unknown")
            fac_lat = float(f.get("FacLat", 0) or 0)
            fac_lon = float(f.get("FacLong", 0) or 0)
            dist = round(haversine(lat, lon, fac_lat, fac_lon), 2) if fac_lat and fac_lon else 999.0
            status = f.get("RCRAComplianceStatus", "") or ""
            # NC trigger: CA facilities with active status
            nc = "CA" in handler_types and status not in ["No Violation Identified", ""]
            sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── EPA FRS/SEMS NPL helper ───────────────────────────────────────────────────

def frs_npl_query(lat, lon, radius_miles, npl_status_filter=None):
    """
    Query EPA FRS NPL ArcGIS layer.
    npl_status_filter: list of NPL status codes to include e.g. ["NPL", "Proposed NPL"]
    """
    radius_meters = radius_miles * 1609.34
    url = "https://geodata.epa.gov/arcgis/rest/services/OEI/FRS_INTERESTS/MapServer/21/query"
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_meters,
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "PRIMARY_NAME,NPL_STATUS_NAME,LATITUDE83,LONGITUDE83",
        "returnGeometry": "true",
        "f": "json"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        sites = []
        for feature in data.get("features", []):
            attrs = feature.get("attributes", {})
            geom = feature.get("geometry", {})
            npl_status = attrs.get("NPL_STATUS_NAME", "") or ""
            if npl_status_filter and npl_status not in npl_status_filter:
                continue
            name = attrs.get("PRIMARY_NAME", "Unknown") or "Unknown"
            fac_lat = float(attrs.get("LATITUDE83", 0) or (geom.get("y", 0)))
            fac_lon = float(attrs.get("LONGITUDE83", 0) or (geom.get("x", 0)))
            dist = round(haversine(lat, lon, fac_lat, fac_lon), 2) if fac_lat else 999.0
            nc = npl_status in ["Currently on the Final NPL", "Proposed for NPL"]
            sites.append({"name": name, "distance": dist, "status": npl_status, "nc": nc})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── SEMS Envirofacts NFRAP/archived helper ────────────────────────────────────

def sems_nfrap_query(lat, lon, radius_miles):
    """Query SEMS archived/NFRAP sites via Envirofacts API filtered to FL."""
    url = "https://data.epa.gov/efservice/sems.envirofacts_site/fk_ref_state_code/equals/FL/site_type/equals/A/rows/0:500/JSON"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        sites = []
        for record in data:
            site_lat = float(record.get("latitude", 0) or 0)
            site_lon = float(record.get("longitude", 0) or 0)
            if not site_lat or not site_lon:
                continue
            dist = haversine(lat, lon, site_lat, site_lon)
            if dist <= radius_miles:
                name = record.get("site_name", "Unknown") or "Unknown"
                sites.append({"name": name, "distance": round(dist, 2), "status": "NFRAP/Archived", "nc": False})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── ERNS helper ───────────────────────────────────────────────────────────────

def erns_query(lat, lon, address_zip):
    """Query ERNS by ZIP code (property-only search per ASTM standard)."""
    url = f"https://data.epa.gov/efservice/erns.erns_inci_vw/zip_code/equals/{address_zip}/rows/0:100/JSON"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        sites = []
        for record in data:
            name = record.get("name_of_reporter", record.get("facility_name", "ERNS Incident")) or "ERNS Incident"
            incident_type = record.get("type_of_incident", "") or ""
            sites.append({"name": name, "distance": 0.0, "status": incident_type, "nc": True})
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── USACE FUDS helper ─────────────────────────────────────────────────────────

def fuds_query(lat, lon, radius_miles):
    """Query USACE FUDS public ArcGIS layer."""
    radius_meters = radius_miles * 1609.34
    url = "https://services7.arcgis.com/n1YM8pTrFmm7L4hs/arcgis/rest/services/FUDS_Projects/FeatureServer/0/query"
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_meters,
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "PROJECT_NAME,PROJECT_STATUS,LATITUDE,LONGITUDE",
        "returnGeometry": "true",
        "f": "json"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        sites = []
        for feature in data.get("features", []):
            attrs = feature.get("attributes", {})
            geom = feature.get("geometry", {})
            name = attrs.get("PROJECT_NAME", "Unknown FUDS") or "Unknown FUDS"
            status = attrs.get("PROJECT_STATUS", "") or ""
            fac_lat = float(attrs.get("LATITUDE", 0) or geom.get("y", 0))
            fac_lon = float(attrs.get("LONGITUDE", 0) or geom.get("x", 0))
            dist = round(haversine(lat, lon, fac_lat, fac_lon), 2) if fac_lat else 999.0
            nc = status.upper() not in ["CLOSED", "COMPLETE", "NO FURTHER ACTION"]
            sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── FDEP service URLs ─────────────────────────────────────────────────────────

FDEP_ERIC    = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/0/query"
FDEP_CHAZ    = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CHAZ_SP/MapServer/0/query"
FDEP_STCM    = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/STCM_SP/MapServer/0/query"
FDEP_SOLID   = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/SolidWaste_SP/MapServer/0/query"
FDEP_BROWN   = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/Brownfields_SP/MapServer/0/query"
FDEP_IC      = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/ICR_SP/MapServer/0/query"

# FDEP cleanup phase codes that trigger a non-compliance bullet (Phase 0-4)
FDEP_NC_PHASES = ["0", "1", "2", "3", "4", "Phase 0", "Phase 1", "Phase 2", "Phase 3", "Phase 4",
                  "PA", "SI", "RI", "FS", "RD"]  # also common abbreviations

# ── Main query endpoint ───────────────────────────────────────────────────────

@app.route("/query", methods=["GET"])
def query():
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
        zipcode = request.args.get("zip", "")
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon are required numeric parameters"}), 400

    results = {}

    # ── 1-mile searches ───────────────────────────────────────────────────────

    # NPL sites (proposed + final)
    npl = frs_npl_query(lat, lon, 1.0,
        npl_status_filter=["Currently on the Final NPL", "Proposed for NPL"])
    results["npl"] = npl

    # FUDS
    results["fuds"] = fuds_query(lat, lon, 1.0)

    # RCRA corrective action
    results["rcra_ca"] = echo_rcra_query(lat, lon, 1.0, "CA")

    # ── 0.5-mile searches ─────────────────────────────────────────────────────

    # Delisted NPL (informational — nc=False)
    delisted = frs_npl_query(lat, lon, 0.5,
        npl_status_filter=["Deleted from the Final NPL"])
    for s in delisted.get("sites", []):
        s["nc"] = False
    results["npl_del"] = delisted

    # CERCLA removals + NFRAP (lumped as "CERCLA sites")
    nfrap = sems_nfrap_query(lat, lon, 0.5)
    results["cercla"] = nfrap

    # RCRA TSD facilities
    results["rcra_tsd"] = echo_rcra_query(lat, lon, 0.5, "TSD")

    # FDEP Hazardous waste (CHAZ)
    chaz_data = fdep_query(FDEP_CHAZ, lat, lon, 0.5)
    chaz_sites = extract_fdep_sites(chaz_data, lat, lon,
        name_field="FAC_NAME",
        status_field="CLEANUP_STATUS",
        nc_statuses=FDEP_NC_PHASES)
    results["haz"] = {"count": len(chaz_sites), "sites": chaz_sites}

    # FDEP ERIC contamination sites (Responsible Party + State Funded + SIS + HW)
    eric_data = fdep_query(FDEP_ERIC, lat, lon, 0.5,
        where_clause="PROGRAM IN ('RESPONSPARTY','STATE','SIS','RCRA','CERCLA')")
    eric_sites = extract_fdep_sites(eric_data, lat, lon,
        name_field="SITE_NAME",
        status_field="CLEANUP_STATUS_TYPE_KEY",
        nc_statuses=FDEP_NC_PHASES)
    results["cont"] = {"count": len(eric_sites), "sites": eric_sites}

    # FDEP Solid Waste
    solid_data = fdep_query(FDEP_SOLID, lat, lon, 0.5)
    solid_sites = extract_fdep_sites(solid_data, lat, lon,
        name_field="FACILITY_NAME",
        status_field="FACILITY_STATUS",
        nc_statuses=FDEP_NC_PHASES)
    results["solid"] = {"count": len(solid_sites), "sites": solid_sites}

    # FDEP Leaking USTs (STCM — contamination monitoring sites)
    stcm_data = fdep_query(FDEP_STCM, lat, lon, 0.5)
    stcm_sites = extract_fdep_sites(stcm_data, lat, lon,
        name_field="SITE_NAME",
        status_field="SITE_STATUS",
        nc_statuses=FDEP_NC_PHASES)
    results["lust"] = {"count": len(stcm_sites), "sites": stcm_sites}

    # FDEP Voluntary cleanup (Drycleaning + Petroleum + Resp Party from ERIC)
    vol_data = fdep_query(FDEP_ERIC, lat, lon, 0.5,
        where_clause="PROGRAM IN ('DRYCLEANING','PETROLEUM','RESPONSPARTY')")
    vol_sites = extract_fdep_sites(vol_data, lat, lon,
        name_field="SITE_NAME",
        status_field="CLEANUP_STATUS_TYPE_KEY",
        nc_statuses=FDEP_NC_PHASES)
    results["vol"] = {"count": len(vol_sites), "sites": vol_sites}

    # FDEP Brownfields
    brown_data = fdep_query(FDEP_BROWN, lat, lon, 0.5)
    brown_sites = extract_fdep_sites(brown_data, lat, lon,
        name_field="SITE_NAME",
        status_field="REMEDIATION",
        nc_statuses=FDEP_NC_PHASES)
    results["brown"] = {"count": len(brown_sites), "sites": brown_sites}

    # ── Adjoining property (~0.15 mi) ─────────────────────────────────────────

    # FDEP registered storage tanks (STCM)
    ust_data = fdep_query(FDEP_STCM, lat, lon, 0.15)
    ust_sites = extract_fdep_sites(ust_data, lat, lon,
        name_field="SITE_NAME",
        status_field="SITE_STATUS",
        nc_statuses=FDEP_NC_PHASES)
    results["ust"] = {"count": len(ust_sites), "sites": ust_sites}

    # RCRA generators (adjoining)
    results["rcra_gen"] = echo_rcra_query(lat, lon, 0.15, "LQG,SQG,VSQG")

    # ── Property only (~0.05 mi) ──────────────────────────────────────────────

    # FDEP Institutional controls
    ic_data = fdep_query(FDEP_IC, lat, lon, 0.05)
    ic_sites = extract_fdep_sites(ic_data, lat, lon,
        name_field="SITE_NAME",
        status_field="IC_STATUS",
        nc_statuses=["Active", "ACTIVE"])
    results["ic"] = {"count": len(ic_sites), "sites": ic_sites}

    # ERNS (by ZIP code)
    if zipcode:
        results["erns"] = erns_query(lat, lon, zipcode)
    else:
        results["erns"] = {"count": 0, "sites": [],
            "note": "ZIP code not provided — ERNS skipped. Add ?zip=XXXXX to query."}

    return jsonify(results)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Phase I ESA Proxy"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
