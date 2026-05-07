# places-scraper

Lead-generation tooling for Kraków, Poland. Finds businesses that **do not have
a standalone website** (Facebook / Instagram / X / TikTok / Linktree as the
listed URL still count as leads in the API scraper), scores them and exports to
CSV.

Two independent implementations live side-by-side:

| script           | strategy                              | output                       |
| ---------------- | ------------------------------------- | ---------------------------- |
| `scraper.py`     | Playwright-driven Google Maps scraper | `krakow_scraped_leads.csv`   |
| `scraper_api.py` | Official Google Places Web Service    | `krakow_leads.csv`           |

They share `places.yaml` / `districts.yaml` (used by the Playwright version) but
otherwise have no overlap. Pick whichever fits your constraints — the API
version is faster and far more reliable, but requires a billable Google Cloud
key. The Playwright version needs no key but is slower and more brittle.

## Prerequisites

- **Python 3.10+** (3.11 recommended; if you use [pyenv](https://github.com/pyenv/pyenv), the repo includes `.python-version` pointing at 3.11).
- Network access on first run (Google Maps + Playwright browser download).

## Local setup (macOS / Linux)

One-shot script (creates `.venv`, installs packages, downloads Chromium):

```bash
cd /path/to/places-scraper
bash setup.sh
```

Then every new terminal session:

```bash
cd /path/to/places-scraper
source .venv/bin/activate
python scraper.py
```

Or install manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

## Local setup (Windows)

In **PowerShell** from the project folder:

```powershell
cd C:\path\to\places-scraper
.\setup.ps1
```

If execution policy blocks the script, run once as Administrator:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

Then activate and run:

```powershell
.\.venv\Scripts\Activate.ps1
python scraper.py
```

## Run

With the venv **activated**:

```bash
python scraper.py                  # headless, default queries
python scraper.py --debug          # visible browser, slower actions
python scraper.py --output leads.csv
python scraper.py --queries "cafes Kraków" "tattoo Kraków"
```

Output defaults to `krakow_scraped_leads.csv` in the current working directory.

Default queries:

- `restaurants Kraków`
- `bars Kraków`
- `beauty salon Kraków`
- `dentist Kraków`
- `gym Kraków`

## Output

CSV file (default `krakow_scraped_leads.csv`) with columns:

| column   | description                                |
| -------- | ------------------------------------------ |
| name     | Business name                              |
| address  | Full street address                        |
| phone    | Phone number, when listed                  |
| rating   | Average rating (float)                     |
| reviews  | Number of reviews (int)                    |
| category | Primary category (e.g. "Restaurant")       |
| website  | Always empty – only no-website leads kept  |
| score    | Lead score (see below)                     |

Rows are sorted by `score` descending.

### Lead scoring

| condition          | points |
| ------------------ | ------ |
| no website         | +5     |
| rating ≥ 4.2       | +3     |
| reviews ≥ 30       | +3     |
| reviews ≥ 100      | +2     |

## Architecture

A single `GoogleMapsScraper` class wraps Playwright and exposes:

- `search(query)` – type a query into the Maps search box.
- `scroll_results()` – scroll the results sidebar until no new entries load.
- `extract_listings()` – return the visible result-card locators.
- `extract_details()` – read name / address / phone / website / rating / reviews
  / category from the open details panel.
- `score_lead(business)` – apply the scoring rules above.

Selectors prefer stable hooks (`role="feed"`, `role="main"`,
`data-item-id="address"`, `a.hfpxzc`) over hashed CSS class names so the
scraper survives most cosmetic Maps redesigns.

## Google Places API (New) version (`scraper_api.py`)

A second, fully separate script that talks to the official
[Places API (New)](https://developers.google.com/maps/documentation/places/web-service/op-overview)
Web Service. No browser, no DOM scraping.

### Setup

```bash
source .venv/bin/activate
pip install -r requirements.txt          # adds `requests`
export GOOGLE_PLACES_API_KEY=AIza...      # required, billable Google Cloud key
```

In Google Cloud Console, enable **Places API (New)** for the project that owns
the key and attach a billing account. The legacy *Places API* does **not**
need to be enabled.

### Run

```bash
python scraper_api.py                              # default: ul. Wrocławska, ~550 m radius
python scraper_api.py --preset krakow              # whole-city disk ~17.5 km from Rynek
python scraper_api.py --preset city                # 15 km from Rynek (narrower than ``krakow``)
python scraper_api.py --types restaurant cafe      # fewer types ⇒ fewer API calls
python scraper_api.py --output leads.csv
python scraper_api.py --preset wroclawska --radius 800   # wider circle on the same street
python scraper_api.py --lat 50.05 --lng 19.93 --radius 400  # any micro-area
python scraper_api.py --max-results 10
python scraper_api.py --cells-file krakow_search_cells.yaml   # ~34 tiles, deduped, one CSV
python scraper_api.py --cells-file krakow_search_cells.yaml --types restaurant  # cheaper multi-cell sweep
```

Default sweep types: **26** Place IDs aligned with ``places.yaml`` (see the
``DEFAULT_TYPES`` tuple in ``scraper_api.py``) plus a generic ``store``
catch-all — ``restaurant``, ``bar``, ``cafe``, ``pub``, ``lounge_bar``,
``night_club``, ``bakery``, ``confectionery``, ``pharmacy``, ``hair_salon``,
``beauty_salon``, ``dentist``, ``veterinary_care``, ``gym``, ``hotel``,
``car_repair``, ``florist``, ``pet_store``, ``bicycle_store``, ``book_store``,
``laundry``, ``funeral_home``, ``body_art_service``, ``liquor_store``,
``jewelry_store``, ``store``.

### Tiling Kraków (`krakow_search_cells.yaml`)

The bundled [`krakow_search_cells.yaml`](krakow_search_cells.yaml) lists **34
overlapping circles** (~1.6–2.2 km radius each) across the built-up area. Smaller
disks mean **more distinct top-20 rankings** per Nearby request than a few
large circles, so you tend to collect more unique businesses before global
dedupe. The script **dedupes by `place_id` globally** and writes one
`krakow_leads.csv`. Each cell’s optional YAML `name` is stored in the CSV
`area` column; if the same place appears in several cells, `area` lists those
labels separated by `"; "`.

**Recommended (one run):**

```bash
python scraper_api.py --cells-file krakow_search_cells.yaml
```

**API calls:** `len(cells) × len(types)` — with the default file and default
types (**26**): **34 × 26 = 884** Nearby Search requests (no Place Details).
List-price ballpark: **884 × ($35 / 1000) ≈ $30.94** if you are entirely past
free tier; often **$0** in practice.

**Run one cell at a time:** copy `lat`, `lng`, and `radius_m` from any block in
the YAML into `--lat` / `--lng` / `--radius`, or keep using `--cells-file` so
dedupe and `area` labelling stay automatic.

Edit the YAML to add/remove cells or tweak `radius_m`. ``--cells-file`` ignores
``--preset`` and ``--lat`` / ``--lng`` / ``--radius``.

**Geographic presets**

| Preset | Area |
| ------ | ---- |
| `wroclawska` (default) | Tight circle on ul. Wrocławska (OSM-derived centre). |
| `city` | 15 km radius from Rynek `(50.0647, 19.9450)`. |
| `krakow` | **17.5 km** radius from the same centre — wider “whole Kraków” sweep (still a circle, not a polygon). |

**API call count:** **one Nearby Search per (area × type)**. Single preset:
**26** calls with default ``--types``. With ``--cells-file`` and *N* cells:
**N × 26** (e.g. 34 cells → **884** calls). Radius does not change the number
of calls.

**Cost estimate (list price):** Nearby Search (New) with our field mask is billed as **Nearby Search Enterprise** — about **$35 USD per 1,000** requests in the first paid tier ([pricing table](https://developers.google.com/maps/billing-and-pricing/pricing#places-pricing)). Examples at list price after free tier: **26 calls ≈ $0.91**; **884 calls (full Kraków cell tiling) ≈ $30.94**. In practice many projects see **$0** (monthly free cap + **$200** Maps Platform credit). Each run logs a hint.

### Output (`krakow_leads.csv`)

| column            | description                                        |
| ----------------- | -------------------------------------------------- |
| name              | `displayName.text` from Places API (New)           |
| area              | Cell label: YAML `name`, or preset name, or `id` with `_` → spaces; merged with `"; "` if deduped from several cells |
| address           | `formattedAddress`                                 |
| phone             | `nationalPhoneNumber`                              |
| rating            | Average rating (float)                             |
| reviews           | `userRatingCount` (int)                            |
| types             | Pipe-joined Google Places type tags                |
| website           | `websiteUri` when it is social-only (FB / IG / X / TikTok / Linktree); empty if none |
| score             | Lead score (+5 when no standalone site, incl. social-only) |
| google_maps_link  | API-provided `googleMapsUri`, or `…?q=place_id:…`  |
| place_id          | Stable Google place identifier                     |

Rows are sorted by `score` descending and deduplicated by `place_id`.

### How it queries the API

1. **One** `POST /v1/places:searchNearby` call per **(search circle × type)**:
   one circle in preset mode, or each cell in ``--cells-file`` mode.
2. The field mask includes `places.websiteUri`, `places.nationalPhoneNumber`,
   `places.rating`, and `places.userRatingCount`, so each call is billed at the
   **Nearby Search Enterprise** SKU (and not at the more expensive
   *Enterprise + Atmosphere* tier).
3. Because the response already contains every field written to the CSV, the
   default flow makes **zero** Place Details calls. `get_place_details()` is
   kept as a documented helper for callers who want to enrich an individual
   record.
4. Places with a **standalone** website (non-social `websiteUri`) are excluded.
   Facebook, Instagram, X (Twitter), TikTok, and Linktree URLs are **kept** as
   leads; that URL is written in the `website` column. Duplicates across types
   are removed by `place_id`.

### Behaviour notes

- The default **Wrocławska** preset is a **single search circle** (~550 m); it
  covers the main OSM-mapped segments of that street but not every fork or
  distant continuation—widen with `--radius` or switch centre with `--lat` /
  `--lng` if you need a different stretch.
- Use ``--cells-file krakow_search_cells.yaml`` for a **tiling** of Kraków (34
  overlapping circles); see the YAML for centres and radii.
- A small sleep (`REQUEST_DELAY_S`) is inserted between every API request.
- Transient errors (HTTP 429 / 5xx) are retried with exponential back-off;
  permanent 4xx errors (`REQUEST_DENIED`, `INVALID_ARGUMENT`) abort that
  search and the script moves on to the next type.
- Locale defaults to `languageCode=pl`, `regionCode=PL` so display names come
  back in Polish where Google has them.
