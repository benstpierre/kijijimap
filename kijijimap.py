#!/usr/bin/env python3
"""
KijijiMap - Scrape Kijiji search results and show listings on a map.
Usage: python3 kijijimap.py "https://www.kijiji.ca/b-cars-trucks/alberta/..."
"""

import sys
import json
import re
import time
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-CA,en;q=0.9",
}


def get_json_ld(html):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string)
            if isinstance(data, dict):
                return data
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        return item
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def scrape_search_page(url):
    """Scrape one page of search results, return list of listing stubs."""
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = get_json_ld(resp.text)
    if not data:
        return []

    items = data.get("itemListElement", [])
    listings = []
    for entry in items:
        # Items can be nested under "item" key or be flat
        item = entry.get("item", entry)
        listing = {
            "title": item.get("name", ""),
            "price": item.get("offers", {}).get("price", ""),
            "url": item.get("url", ""),
            "image": item.get("image", ""),
            "mileage": "",
            "year": item.get("vehicleModelDate", ""),
            "make": "",
            "model": item.get("model", ""),
        }
        brand = item.get("brand", {})
        if isinstance(brand, dict):
            listing["make"] = brand.get("name", "")
        mileage = item.get("mileageFromOdometer")
        if isinstance(mileage, dict):
            listing["mileage"] = mileage.get("value", "")
        if listing["url"]:
            listings.append(listing)
    return listings


def scrape_all_search_pages(search_url, max_pages=10):
    """Paginate through search results."""
    all_listings = []
    for page in range(1, max_pages + 1):
        if page == 1:
            url = search_url
        else:
            parsed = urlparse(search_url)
            path_parts = parsed.path.rstrip("/").rsplit("/", 1)
            if len(path_parts) == 2:
                new_path = f"{path_parts[0]}/page-{page}/{path_parts[1]}"
            else:
                new_path = f"{parsed.path}/page-{page}"
            url = parsed._replace(path=new_path).geturl()

        print(f"  Fetching search page {page}...")
        try:
            listings = scrape_search_page(url)
        except Exception as e:
            print(f"  Error on page {page}: {e}")
            break

        if not listings:
            print(f"  No more listings on page {page}, stopping.")
            break

        all_listings.extend(listings)
        print(f"  Found {len(listings)} listings (total: {len(all_listings)})")

        if page < max_pages:
            time.sleep(1)

    return all_listings


def scrape_listing_detail(listing):
    """Fetch individual listing page to get lat/lng."""
    try:
        resp = requests.get(listing["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()

        # The availableAtOrFrom with lat/lng is in the JSON-LD but sometimes
        # nested differently. Search for it via regex as fallback.
        data = get_json_ld(resp.text)
        lat = lng = None
        postal = address = ""

        if data:
            loc = data.get("availableAtOrFrom", data.get("contentLocation", {}))
            if isinstance(loc, dict):
                lat = loc.get("latitude")
                lng = loc.get("longitude")
                addr = loc.get("address", {})
                if isinstance(addr, dict):
                    postal = addr.get("postalCode", "")
                    address = addr.get("streetAddress", "") or addr.get("addressLocality", "")

        # Fallback: regex search for coordinates
        if not lat or not lng:
            m = re.search(r'"latitude":\s*([\d.-]+).*?"longitude":\s*([\d.-]+)', resp.text)
            if m:
                lat, lng = float(m.group(1)), float(m.group(2))

        if not postal:
            m = re.search(r'["\s]([A-Z]\d[A-Z]\s?\d[A-Z]\d)["\s,]', resp.text)
            if m:
                postal = m.group(1)

        if lat and lng:
            listing["lat"] = float(lat)
            listing["lng"] = float(lng)
            listing["postal"] = postal
            listing["address"] = address
            return listing
    except Exception as e:
        print(f"    Error: {e}")
    return None


def scrape_all_details(listings):
    """Fetch details for all listings with progress."""
    results = []
    total = len(listings)
    for i, listing in enumerate(listings):
        label = listing["title"][:50]
        print(f"  [{i+1}/{total}] {label}...", end="", flush=True)
        result = scrape_listing_detail(listing)
        if result:
            results.append(result)
            print(f" ({result['lat']:.2f}, {result['lng']:.2f})")
        else:
            print(" no location")
        if i < total - 1:
            time.sleep(0.5)
    return results


# Calgary neighborhood zones - approximate bounding boxes [south, west, north, east]
# Green = preferred areas, Red = avoid areas
NEIGHBORHOODS = {
    "prefer": [
        {"name": "Mount Royal / Elbow Park", "bounds": [50.995, -114.10, 51.025, -114.06]},
        {"name": "Britannia / Elboya", "bounds": [50.985, -114.10, 50.998, -114.06]},
        {"name": "Pump Hill / Woodbine", "bounds": [50.93, -114.12, 50.96, -114.05]},
        {"name": "Aspen Woods / Springbank Hill", "bounds": [50.98, -114.20, 51.02, -114.12]},
        {"name": "Signal Hill / Strathcona Park", "bounds": [50.98, -114.18, 51.00, -114.12]},
        {"name": "Tuscany / Rocky Ridge", "bounds": [51.12, -114.24, 51.16, -114.18]},
        {"name": "Edgemont / Hamptons", "bounds": [51.12, -114.18, 51.16, -114.12]},
        {"name": "Varsity / Dalhousie", "bounds": [51.08, -114.16, 51.12, -114.10]},
        {"name": "Lake Bonavista / Willow Park", "bounds": [50.94, -114.06, 50.97, -114.00]},
        {"name": "McKenzie Towne / Cranston", "bounds": [50.88, -114.02, 50.93, -113.94]},
        {"name": "Brentwood / Charleswood", "bounds": [51.08, -114.14, 51.10, -114.10]},
        {"name": "Garrison Woods / Marda Loop", "bounds": [51.02, -114.10, 51.04, -114.06]},
        {"name": "Altadore / South Calgary", "bounds": [51.02, -114.10, 51.04, -114.06]},
        {"name": "Panorama Hills / Country Hills", "bounds": [51.13, -114.10, 51.17, -114.02]},
        {"name": "Airdrie", "bounds": [51.25, -114.05, 51.30, -113.96]},
        {"name": "Okotoks", "bounds": [50.71, -113.99, 50.74, -113.95]},
        {"name": "Cochrane", "bounds": [51.17, -114.50, 51.20, -114.44]},
        # Edmonton - preferred
        {"name": "Glenora / Westmount", "bounds": [53.53, -113.55, 53.56, -113.52]},
        {"name": "Crestwood / Parkview / Laurier Heights", "bounds": [53.51, -113.57, 53.54, -113.53]},
        {"name": "Windermere / Keswick", "bounds": [53.43, -113.58, 53.47, -113.53]},
        {"name": "Ambleside / Grange", "bounds": [53.47, -113.58, 53.50, -113.53]},
        {"name": "Riverbend / Terwillegar", "bounds": [53.45, -113.58, 53.50, -113.52]},
        {"name": "Magrath Heights / Cameron Heights", "bounds": [53.44, -113.59, 53.48, -113.55]},
        {"name": "Wolf Willow / Brander Gardens", "bounds": [53.46, -113.56, 53.49, -113.52]},
        {"name": "Strathcona / Bonnie Doon", "bounds": [53.51, -113.49, 53.53, -113.45]},
        {"name": "Summerside / Ellerslie", "bounds": [53.40, -113.53, 53.43, -113.47]},
        {"name": "St. Albert", "bounds": [53.61, -113.65, 53.65, -113.58]},
        {"name": "Sherwood Park", "bounds": [53.51, -113.35, 53.55, -113.27]},
        {"name": "Spruce Grove / Stony Plain", "bounds": [53.53, -113.93, 53.56, -113.85]},
    ],
    "avoid": [
        # Calgary - avoid
        {"name": "Dover", "bounds": [51.02, -113.99, 51.04, -113.96]},
        {"name": "Forest Lawn / Penbrooke", "bounds": [51.04, -113.99, 51.06, -113.95]},
        {"name": "Temple", "bounds": [51.06, -113.97, 51.09, -113.94]},
        {"name": "Marlborough", "bounds": [51.04, -113.97, 51.07, -113.94]},
        {"name": "Falconridge / Castleridge", "bounds": [51.09, -113.97, 51.11, -113.93]},
        {"name": "Pineridge", "bounds": [51.06, -113.96, 51.09, -113.93]},
        # Edmonton - avoid
        {"name": "Alberta Ave / Norwood", "bounds": [53.56, -113.49, 53.58, -113.47]},
        {"name": "McCauley / Boyle Street", "bounds": [53.54, -113.49, 53.56, -113.46]},
        {"name": "Central McDougall / Queen Mary Park", "bounds": [53.55, -113.51, 53.57, -113.49]},
        {"name": "Abbottsfield / Rundle", "bounds": [53.57, -113.43, 53.59, -113.39]},
    ],
}


def generate_html(listings, search_url):
    """Generate the map HTML page."""
    listings_json = json.dumps(listings)
    neighborhoods_json = json.dumps(NEIGHBORHOODS)
    return f"""<!DOCTYPE html>
<html>
<head>
<title>Uncle Bernie's Used Car Finder</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
  #header {{
    background: #1a1a2e; color: white; padding: 10px 20px;
    display: flex; justify-content: space-between; align-items: center;
    font-size: 14px; flex-wrap: wrap; gap: 8px;
  }}
  #header a {{ color: #7bf; text-decoration: none; }}
  #controls {{ display: flex; gap: 12px; align-items: center; }}
  #controls label {{ cursor: pointer; user-select: none; }}
  #controls input[type=checkbox] {{ margin-right: 4px; }}
  #map {{ height: calc(100vh - 44px); width: 100%; }}
  .listing-popup {{
    max-width: 300px;
    font-size: 13px;
  }}
  .listing-popup img {{
    width: 100%; max-height: 160px; object-fit: cover;
    border-radius: 4px; margin-bottom: 6px;
  }}
  .listing-popup .title {{
    font-weight: bold; margin-bottom: 4px;
  }}
  .listing-popup .price {{ color: #2a7; font-weight: bold; font-size: 16px; }}
  .listing-popup .meta {{ color: #666; margin-top: 4px; font-size: 12px; }}
  .listing-popup .zone-tag {{
    display: inline-block; padding: 2px 6px; border-radius: 3px;
    font-size: 11px; font-weight: bold; margin-top: 4px;
  }}
  .listing-popup .zone-prefer {{ background: #d4edda; color: #155724; }}
  .listing-popup .zone-avoid {{ background: #f8d7da; color: #721c24; }}
  .listing-popup .link {{ margin-top: 6px; }}
  .listing-popup .link a {{ color: #07f; }}
  .legend {{
    background: white; padding: 10px 14px; border-radius: 6px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.3); font-size: 12px;
    line-height: 1.8;
  }}
  .legend-color {{
    display: inline-block; width: 12px; height: 12px;
    border-radius: 50%; margin-right: 6px; vertical-align: middle;
  }}
</style>
</head>
<body>
<div id="header">
  <span>Uncle Bernie's Used Car Finder - <strong>{len(listings)}</strong> listings</span>
  <div id="controls">
    <label><input type="checkbox" id="showZones" checked> Show zones</label>
    <a href="{search_url}" target="_blank">Open Kijiji search</a>
  </div>
</div>
<div id="map"></div>
<script>
const listings = {listings_json};
const neighborhoods = {neighborhoods_json};

const map = L.map('map').setView([51.0447, -114.0719], 11);

L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '&copy; OSM',
  maxZoom: 18,
}}).addTo(map);

// Draw neighborhood zones
const zoneLayers = L.layerGroup().addTo(map);

function drawZones() {{
  zoneLayers.clearLayers();
  neighborhoods.prefer.forEach(n => {{
    L.rectangle(
      [[n.bounds[0], n.bounds[1]], [n.bounds[2], n.bounds[3]]],
      {{ color: '#28a745', weight: 2, fillOpacity: 0.10, dashArray: '5,5' }}
    ).bindTooltip(n.name, {{ sticky: true, className: 'zone-tooltip' }}).addTo(zoneLayers);
  }});
  neighborhoods.avoid.forEach(n => {{
    L.rectangle(
      [[n.bounds[0], n.bounds[1]], [n.bounds[2], n.bounds[3]]],
      {{ color: '#dc3545', weight: 2, fillOpacity: 0.10, dashArray: '5,5' }}
    ).bindTooltip(n.name, {{ sticky: true, className: 'zone-tooltip' }}).addTo(zoneLayers);
  }});
}}

drawZones();

document.getElementById('showZones').addEventListener('change', function() {{
  if (this.checked) {{ zoneLayers.addTo(map); }} else {{ map.removeLayer(zoneLayers); }}
}});

// Determine if a listing is in a zone
function getZone(lat, lng) {{
  for (const n of neighborhoods.prefer) {{
    if (lat >= n.bounds[0] && lat <= n.bounds[2] && lng >= n.bounds[1] && lng <= n.bounds[3])
      return {{ type: 'prefer', name: n.name }};
  }}
  for (const n of neighborhoods.avoid) {{
    if (lat >= n.bounds[0] && lat <= n.bounds[2] && lng >= n.bounds[1] && lng <= n.bounds[3])
      return {{ type: 'avoid', name: n.name }};
  }}
  return null;
}}

function formatPrice(p) {{
  const n = parseFloat(p);
  if (!n) return 'Price N/A';
  return '$' + n.toLocaleString();
}}

function formatKm(km) {{
  const n = parseFloat(km);
  if (!n) return '';
  return n.toLocaleString() + ' km';
}}

const markers = L.featureGroup();

listings.forEach(l => {{
  const zone = getZone(l.lat, l.lng);
  let color = '#3388ff'; // neutral blue
  let radius = 7;
  if (zone) {{
    if (zone.type === 'prefer') {{ color = '#28a745'; radius = 9; }}
    if (zone.type === 'avoid') {{ color = '#dc3545'; radius = 6; }}
  }}

  const marker = L.circleMarker([l.lat, l.lng], {{
    radius: radius,
    fillColor: color,
    color: '#fff',
    weight: 2,
    opacity: 1,
    fillOpacity: 0.85,
  }});

  const imgHtml = l.image ? `<img src="${{l.image}}" alt="">` : '';
  const kmHtml = l.mileage ? `${{formatKm(l.mileage)}}` : '';
  const yearMake = [l.year, l.make, l.model].filter(Boolean).join(' ');
  const zoneHtml = zone
    ? `<span class="zone-tag zone-${{zone.type}}">${{zone.type === 'prefer' ? 'Good area' : 'Caution'}}: ${{zone.name}}</span>`
    : '';

  marker.bindPopup(`
    <div class="listing-popup">
      ${{imgHtml}}
      <div class="title">${{l.title}}</div>
      <div class="price">${{formatPrice(l.price)}}</div>
      <div class="meta">${{[yearMake, kmHtml, l.postal].filter(Boolean).join(' &bull; ')}}</div>
      ${{zoneHtml}}
      <div class="link"><a href="${{l.url}}" target="_blank">View on Kijiji &rarr;</a></div>
    </div>
  `);

  markers.addLayer(marker);
}});

markers.addTo(map);

if (listings.length > 0) {{
  map.fitBounds(markers.getBounds().pad(0.1));
}}

// Legend
const legend = L.control({{ position: 'bottomright' }});
legend.onAdd = function() {{
  const div = L.DomUtil.create('div', 'legend');
  div.innerHTML = `
    <strong>Legend</strong><br>
    <span class="legend-color" style="background:#28a745"></span> Preferred area<br>
    <span class="legend-color" style="background:#3388ff"></span> Neutral<br>
    <span class="legend-color" style="background:#dc3545"></span> Caution area
  `;
  return div;
}};
legend.addTo(map);
</script>
</body>
</html>"""


CACHE_PATH = "/usr/local/code/throwaway/kijijimap/listings_cache.json"
OUT_PATH = "/usr/local/code/throwaway/kijijimap/map.html"


def main():
    # --regen: rebuild map from cached listings (no re-scraping)
    if len(sys.argv) >= 2 and sys.argv[1] == "--regen":
        try:
            with open(CACHE_PATH) as f:
                cached = json.load(f)
            search_url = cached["search_url"]
            listings = cached["listings"]
        except (FileNotFoundError, KeyError, json.JSONDecodeError):
            print("No cached listings found. Run a full scrape first.")
            sys.exit(1)

        print(f"Regenerating map from {len(listings)} cached listings...")
        html = generate_html(listings, search_url)
        with open(OUT_PATH, "w") as f:
            f.write(html)
        print(f"Map written to {OUT_PATH}")
        webbrowser.open(f"file://{OUT_PATH}")
        return

    if len(sys.argv) < 2:
        print("Usage:")
        print('  python3 kijijimap.py <kijiji-search-url> [max-pages]  # scrape and map')
        print('  python3 kijijimap.py --regen                          # rebuild map from cache')
        sys.exit(1)

    search_url = sys.argv[1]
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    print(f"\n=== Uncle Bernie's Used Car Finder ===")
    print(f"Search URL: {search_url}")
    print(f"Max pages: {max_pages}\n")

    print("Phase 1: Scraping search results...")
    listings = scrape_all_search_pages(search_url, max_pages)
    print(f"\nFound {len(listings)} total listings.\n")

    if not listings:
        print("No listings found. Check the URL and try again.")
        sys.exit(1)

    print("Phase 2: Fetching location data for each listing...")
    located = scrape_all_details(listings)
    print(f"\n{len(located)} of {len(listings)} listings have map coordinates.\n")

    if not located:
        print("No listings with location data found.")
        sys.exit(1)

    # Cache listings for --regen
    with open(CACHE_PATH, "w") as f:
        json.dump({"search_url": search_url, "listings": located}, f)
    print(f"Cached {len(located)} listings to {CACHE_PATH}")

    html = generate_html(located, search_url)
    with open(OUT_PATH, "w") as f:
        f.write(html)
    print(f"Map written to {OUT_PATH}")

    port = 8787

    class QuietHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory="/usr/local/code/throwaway/kijijimap", **kwargs)
        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), QuietHandler)
    print(f"\nOpen: http://127.0.0.1:{port}/map.html")
    print("Press Ctrl+C to stop.\n")

    webbrowser.open(f"http://127.0.0.1:{port}/map.html")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDone.")


if __name__ == "__main__":
    main()
