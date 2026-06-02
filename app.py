"""
Phase I ESA Database Proxy — v3
Fixes: FDEP coordinate projection (WGS84 → EPSG:3086 Albers), correct field names,
correct service URLs, fixed SEMS/CERCLA endpoint, corrected NC status logic.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import math

app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

@app.route('/')
def index():
    return app.send_static_file('index.html')

# ── Haversine distance (miles) ────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 3958.8
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# ── WGS84 → Florida GDL Albers (EPSG:3086) ───────────────────────────────────
# Manual forward projection so we don't need pyproj on Render free tier

def wgs84_to_albers_fl(lat_deg, lon_deg):
    """
    Project WGS84 lat/lon to NAD83 Florida GDL Albers (EPSG:3086).
    Parameters: std parallel 1=24, std parallel 2=31.5, central meridian=-84,
    lat of origin=24, false easting=400000, false northing=0.
    Close enough for a radius search buffer.
    """
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = 2*f - f*f
    e = math.sqrt(e2)

    phi1 = math.radians(24.0)
    phi2 = math.radians(31.5)
    phi0 = math.radians(24.0)
    lam0 = math.radians(-84.0)
    FE = 400000.0
    FN = 0.0

    phi = math.radians(lat_deg)
    lam = math.radians(lon_deg)

    def m(phi_r):
        s = math.sin(phi_r)
        return math.cos(phi_r) / math.sqrt(1 - e2 * s*s)

    def q(phi_r):
        s = math.sin(phi_r)
        return (1-e2) * (s/(1-e2*s*s) - (1/(2*e)) * math.log((1-e*s)/(1+e*s)))

    m1 = m(phi1); m2 = m(phi2)
    q0 = q(phi0); q1 = q(phi1); q2 = q(phi2); qp = q(phi)

    n = (m1*m1 - m2*m2) / (q2 - q1)
    C = m1*m1 + n*q1
    rho0 = a * math.sqrt(C - n*q0) / n

    rho = a * math.sqrt(C - n*qp) / n
    theta = n * (lam - lam0)

    x = FE + rho * math.sin(theta)
    y = FN + rho0 - rho * math.cos(theta)
    return x, y

# ── FDEP ArcGIS spatial query helper ─────────────────────────────────────────

def fdep_query(service_url, lat, lon, radius_miles, where_clause="1=1", out_fields="*"):
    """
    Query FDEP ArcGIS layer using projected coordinates (EPSG:3086).
    FDEP services use Florida GDL Albers — must project before querying.
    """
    x, y = wgs84_to_albers_fl(lat, lon)
    radius_meters = radius_miles * 1609.34
    params = {
        "geometry": f"{x},{y}",
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_meters,
        "inSR": "3086",
        "outSR": "4326",   # return in WGS84 so we can compute haversine
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
    sites = []
    for feature in data.get("features", []):
        attrs = feature.get("attributes", {})
        geom  = feature.get("geometry", {})
        name  = attrs.get(name_field) or "Unknown Site"
        dist  = 999.0
        if geom and "x" in geom and "y" in geom:
            dist = round(haversine(lat, lon, geom["y"], geom["x"]), 2)
        status = str(attrs.get(status_field, "") or "") if status_field else ""
        nc = bool(nc_statuses and status in nc_statuses)
        sites.append({"name": str(name), "distance": dist, "status": status, "nc": nc})
    sites.sort(key=lambda s: s["distance"])
    return sites

# ── FDEP service URLs (verified) ──────────────────────────────────────────────

FDEP_ERIC  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/8/query"
FDEP_CHAZ  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CHAZ/MapServer/0/query"
FDEP_STCM_LUST = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/2/query"   # PCTS discharges = leaking tanks
FDEP_STCM_UST  = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/DWM_STCM/MapServer/1/query"   # Registered tanks
FDEP_SOLID = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/SolidWaste_SP/MapServer/0/query"
FDEP_BROWN = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/CLEANUP_SP/MapServer/8/query"     # Brownfields filtered from ERIC
FDEP_IC    = "https://ca.dep.state.fl.us/arcgis/rest/services/OpenData/ICR_SP/MapServer/0/query"

# FDEP PROGRAM_STATUS values that mean NOT complete (trigger NC bullet)
FDEP_NC_STATUSES = {"ACTIVE", "AWAITFUND", "AWAITSITEACCESS", "INPROCESS", "ELIGREVIEW"}

# ── EPA ECHO RCRA helper ───────────────────────────────────────────────────────

def echo_rcra_query(lat, lon, radius_miles, handler_types):
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
            name    = f.get("FacName", "Unknown")
            fac_lat = float(f.get("FacLat",  0) or 0)
            fac_lon = float(f.get("FacLong", 0) or 0)
            dist    = round(haversine(lat, lon, fac_lat, fac_lon), 2) if fac_lat else 999.0
            status  = f.get("RCRAComplianceStatus", "") or ""
            # CA facilities: NC if not "No Violation Identified"
            nc = ("CA" in handler_types) and (status not in ["No Violation Identified", ""])
            sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── EPA FRS/SEMS NPL helper ───────────────────────────────────────────────────

def frs_npl_query(lat, lon, radius_miles, npl_status_filter=None):
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
            attrs      = feature.get("attributes", {})
            geom       = feature.get("geometry", {})
            npl_status = attrs.get("NPL_STATUS_NAME", "") or ""
            if npl_status_filter and npl_status not in npl_status_filter:
                continue
            name    = attrs.get("PRIMARY_NAME", "Unknown") or "Unknown"
            fac_lat = float(attrs.get("LATITUDE83",  0) or geom.get("y", 0))
            fac_lon = float(attrs.get("LONGITUDE83", 0) or geom.get("x", 0))
            dist    = round(haversine(lat, lon, fac_lat, fac_lon), 2) if fac_lat else 999.0
            nc      = npl_status in ["Currently on the Final NPL", "Proposed for NPL"]
            sites.append({"name": name, "distance": dist, "status": npl_status, "nc": nc})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── SEMS CERCLA/NFRAP helper ──────────────────────────────────────────────────

def sems_cercla_query(lat, lon, radius_miles):
    """
    Query SEMS for active non-NPL CERCLA sites in FL via Envirofacts.
    Uses the CERCLA non-NPL FRS layer (NASA HIFLD mirror).
    Falls back to Envirofacts efservice if primary fails.
    """
    radius_meters = radius_miles * 1609.34
    # Try FRS CERCLA non-NPL ArcGIS layer first
    url = "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/FRS_INTERESTS/FeatureServer/3/query"
    params = {
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "spatialRel": "esriSpatialRelIntersects",
        "distance": radius_meters,
        "inSR": "4326",
        "outSR": "4326",
        "outFields": "PRIMARY_NAME,LATITUDE83,LONGITUDE83,ACTIVE_STATUS",
        "returnGeometry": "true",
        "f": "json"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "error" not in data:
            sites = []
            for feature in data.get("features", []):
                attrs   = feature.get("attributes", {})
                geom    = feature.get("geometry", {})
                name    = attrs.get("PRIMARY_NAME", "Unknown CERCLA Site") or "Unknown CERCLA Site"
                fac_lat = float(attrs.get("LATITUDE83",  0) or geom.get("y", 0))
                fac_lon = float(attrs.get("LONGITUDE83", 0) or geom.get("x", 0))
                dist    = round(haversine(lat, lon, fac_lat, fac_lon), 2) if fac_lat else 999.0
                status  = attrs.get("ACTIVE_STATUS", "Active") or "Active"
                nc      = status.upper() not in ["ARCHIVED", "INACTIVE", "NFRAP"]
                sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
            sites.sort(key=lambda s: s["distance"])
            return {"count": len(sites), "sites": sites}
    except Exception:
        pass

    # Fallback: Envirofacts SEMS by county (approximate)
    try:
        url2 = "https://data.epa.gov/efservice/SEMS_ACTIVE_SITES/STATE_CODE/FL/rows/0:500/JSON"
        r2 = requests.get(url2, timeout=15)
        r2.raise_for_status()
        data2 = r2.json()
        sites = []
        for record in data2:
            site_lat = float(record.get("LATITUDE", 0) or 0)
            site_lon = float(record.get("LONGITUDE", 0) or 0)
            if not site_lat or not site_lon:
                continue
            dist = haversine(lat, lon, site_lat, site_lon)
            if dist <= radius_miles:
                name = record.get("SITE_NAME", "Unknown") or "Unknown"
                sites.append({"name": name, "distance": round(dist, 2), "status": "Active CERCLA", "nc": True})
        sites.sort(key=lambda s: s["distance"])
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── ERNS helper ───────────────────────────────────────────────────────────────

def erns_query(zipcode):
    if not zipcode:
        return {"count": 0, "sites": [], "note": "ZIP not provided"}
    url = f"https://data.epa.gov/efservice/ERNS_INCIDENTS/ZIP_CODE/{zipcode}/rows/0:100/JSON"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        sites = []
        for record in data:
            name = record.get("FACILITY_NAME") or record.get("COMPANY_NAME") or "ERNS Incident"
            incident = record.get("INCIDENT_TYPE_DESCRIPTION", "") or ""
            sites.append({"name": name, "distance": 0.0, "status": incident, "nc": True})
        return {"count": len(sites), "sites": sites}
    except Exception as e:
        return {"count": 0, "sites": [], "error": str(e)}

# ── USACE FUDS helper ─────────────────────────────────────────────────────────

def fuds_query(lat, lon, radius_miles):
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
            attrs   = feature.get("attributes", {})
            geom    = feature.get("geometry", {})
            name    = attrs.get("PROJECT_NAME", "Unknown FUDS") or "Unknown FUDS"
            status  = attrs.get("PROJECT_STATUS", "") or ""
            fac_lat = float(attrs.get("LATITUDE",  0) or geom.get("y", 0))
            fac_lon = float(attrs.get("LONGITUDE", 0) or geom.get("x", 0))
            dist    = round(haversine(lat, lon, fac_lat, fac_lon), 2) if fac_lat else 999.0
            nc      = status.upper() not in ["CLOSED", "COMPLETE", "NO FURTHER ACTION", "NFA"]
            sites.append({"name": name, "distance": dist, "status": status, "nc": nc})
        sites.sort(key=lambda s: s["distance"])
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
        return jsonify({"error": "lat and lon are required numeric parameters"}), 400

    zipcode = request.args.get("zip", "")
    results = {}

    # ── 1-mile ────────────────────────────────────────────────────────────────
    results["npl"]     = frs_npl_query(lat, lon, 1.0,
                            npl_status_filter=["Currently on the Final NPL", "Proposed for NPL"])
    results["fuds"]    = fuds_query(lat, lon, 1.0)
    results["rcra_ca"] = echo_rcra_query(lat, lon, 1.0, "CA")

    # ── 0.5-mile ──────────────────────────────────────────────────────────────
    delisted = frs_npl_query(lat, lon, 0.5,
                   npl_status_filter=["Deleted from the Final NPL"])
    for s in delisted.get("sites", []):
        s["nc"] = False   # informational only
    results["npl_del"] = delisted

    results["cercla"] = sems_cercla_query(lat, lon, 0.5)

    results["rcra_tsd"] = echo_rcra_query(lat, lon, 0.5, "TSD")

    # FDEP CHAZ — all hazardous waste facilities (layer 0 = all CHAZ facilities)
    chaz_data  = fdep_query(FDEP_CHAZ, lat, lon, 0.5, out_fields="FAC_NAME,FAC_STATUS,FAC_INS_TYPE")
    chaz_sites = extract_fdep_sites(chaz_data, lat, lon,
                     name_field="FAC_NAME",
                     status_field="FAC_STATUS",
                     nc_statuses={"ACTIVE"})
    results["haz"] = {"count": len(chaz_sites), "sites": chaz_sites}

    # FDEP ERIC contamination sites (excl. brownfields, drycleaning, petroleum which get own categories)
    cont_data  = fdep_query(FDEP_ERIC, lat, lon, 0.5,
                     where_clause="PROGRAM_TYPE IN ('RESPONSPARTY','STATE','SIS','RCRA','CERCLA','FEDERAL','NPL','SRP','SOLCP')",
                     out_fields="SITE_NAME,PROGRAM_STATUS,PROGRAM_TYPE,SITE_STATUS")
    cont_sites = extract_fdep_sites(cont_data, lat, lon,
                     name_field="SITE_NAME",
                     status_field="PROGRAM_STATUS",
                     nc_statuses=FDEP_NC_STATUSES)
    results["cont"] = {"count": len(cont_sites), "sites": cont_sites}

    # FDEP Solid Waste
    solid_data  = fdep_query(FDEP_SOLID, lat, lon, 0.5, out_fields="FACILITY_NAME,FACILITY_STATUS,FACILITY_TYPE")
    solid_sites = extract_fdep_sites(solid_data, lat, lon,
                      name_field="FACILITY_NAME",
                      status_field="FACILITY_STATUS",
                      nc_statuses={"ACTIVE", "OPEN"})
    results["solid"] = {"count": len(solid_sites), "sites": solid_sites}

    # FDEP Leaking USTs — PCTS petroleum discharge sites (layer 2)
    lust_data  = fdep_query(FDEP_STCM_LUST, lat, lon, 0.5, out_fields="SITE_NAME,SITE_STATUS,DISCHARGE_DATE")
    lust_sites = extract_fdep_sites(lust_data, lat, lon,
                     name_field="SITE_NAME",
                     status_field="SITE_STATUS",
                     nc_statuses={"OPEN", "ACTIVE", "INPROCESS"})
    results["lust"] = {"count": len(lust_sites), "sites": lust_sites}

    # FDEP Voluntary cleanup — drycleaning + petroleum restoration from ERIC
    vol_data  = fdep_query(FDEP_ERIC, lat, lon, 0.5,
                    where_clause="PROGRAM_TYPE IN ('DRYCLEANING','PETROLEUM')",
                    out_fields="SITE_NAME,PROGRAM_STATUS,PROGRAM_TYPE")
    vol_sites = extract_fdep_sites(vol_data, lat, lon,
                    name_field="SITE_NAME",
                    status_field="PROGRAM_STATUS",
                    nc_statuses=FDEP_NC_STATUSES)
    results["vol"] = {"count": len(vol_sites), "sites": vol_sites}

    # FDEP Brownfields — filtered from ERIC
    brown_data  = fdep_query(FDEP_BROWN, lat, lon, 0.5,
                      where_clause="PROGRAM_TYPE='BROWNFIELDS' OR PROGRAM LIKE '%Brownfield%'",
                      out_fields="SITE_NAME,PROGRAM_STATUS,PROGRAM_TYPE")
    brown_sites = extract_fdep_sites(brown_data, lat, lon,
                      name_field="SITE_NAME",
                      status_field="PROGRAM_STATUS",
                      nc_statuses=FDEP_NC_STATUSES)
    results["brown"] = {"count": len(brown_sites), "sites": brown_sites}

    # ── Adjoining (~0.15 mi) ──────────────────────────────────────────────────
    ust_data  = fdep_query(FDEP_STCM_UST, lat, lon, 0.15, out_fields="SITE_NAME,SITE_STATUS,TANK_STATUS")
    ust_sites = extract_fdep_sites(ust_data, lat, lon,
                    name_field="SITE_NAME",
                    status_field="SITE_STATUS",
                    nc_statuses={"OPEN", "ACTIVE"})
    results["ust"] = {"count": len(ust_sites), "sites": ust_sites}

    results["rcra_gen"] = echo_rcra_query(lat, lon, 0.15, "LQG,SQG,VSQG")

    # ── Property only (~0.05 mi) ──────────────────────────────────────────────
    ic_data  = fdep_query(FDEP_IC, lat, lon, 0.05, out_fields="SITE_NAME,IC_STATUS,MECHANISM_TYPE")
    ic_sites = extract_fdep_sites(ic_data, lat, lon,
                   name_field="SITE_NAME",
                   status_field="IC_STATUS",
                   nc_statuses={"ACTIVE", "Active"})
    results["ic"] = {"count": len(ic_sites), "sites": ic_sites}

    results["erns"] = erns_query(zipcode)

    return jsonify(results)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Phase I ESA Proxy", "version": "3.0"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
