"""
Google Places API (New) lead-generation scraper.

Sibling of ``scraper.py`` (Playwright-based). This script does *not* drive a
browser; it talks directly to the official Places API (New) Web Service:

* Nearby Search (New)  ``POST /v1/places:searchNearby``  – discover places
* Place Details (New)  ``GET  /v1/places/{place_id}``    – on-demand fallback

It keeps businesses without a **standalone** website (Facebook / Instagram /
X / TikTok / Linktree URLs count as social-only and are still exported with
that URL in the CSV), scores them as web-design leads, and writes
``krakow_leads.csv``.

Cost model
----------
The new API uses **field-mask billing**: a request is billed at the highest SKU
tier any requested field belongs to. Our default field mask requests
``rating``, ``userRatingCount``, ``nationalPhoneNumber`` and ``websiteUri`` from
*Nearby Search*, which puts the call in the **Nearby Search Enterprise** SKU.
Crucially, every field we need is returned by that one Nearby request, so the
default flow makes **zero** Place Details calls — far cheaper than the legacy
two-stage flow (Nearby + Details + Basic + Contact + Atmosphere).

Setup
-----
    export GOOGLE_PLACES_API_KEY=AIza...     # required, billable Google Cloud key
    pip install -r requirements.txt
    python scraper_api.py

Enable **Places API (New)** for the project that owns the key (the legacy
*Places API* is *not* required for this script).

CLI
---
    python scraper_api.py                          # default: ul. Wrocławska, tight radius
    python scraper_api.py --cells-file krakow_search_cells.yaml  # all Kraków tiles, one CSV
    python scraper_api.py --types restaurant cafe  # custom Places types
    python scraper_api.py --lat … --lng … --radius 300  # any micro-area

Call budget
-----------
Single-area mode: **one** Nearby Search per ``--types`` entry (default list
length is ``len(DEFAULT_TYPES)``, currently **26** — see tuple in source).
With ``--cells-file``, multiply by the number of **cells** in the YAML
(e.g. 10 cells × 26 types = **260** calls). There are **no** Place Details calls
on the default path. The script logs a **list-price USD hint** per run (Nearby
Search Enterprise ≈ $35/1000 after free tier); many accounts still see $0 due
to the monthly free cap and $200 Maps credit.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import pandas as pd
import requests
import yaml


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# --- Geographic presets (Nearby Search uses a circle: centre + radius metres)

# Whole-city presets (Nearby Search uses a circle from Rynek area — not a perfect
# match to administrative boundaries, but covers the dense urban footprint).

KRAKOW_CITY_CENTER_LAT = 50.0647
KRAKOW_CITY_CENTER_LNG = 19.9450

# Tighter disk (~30 km diameter) — legacy preset name ``city``.
CITY_WIDE_RADIUS_M = 15_000

# Wider disk (~35 km diameter) — better for “all of Kraków” including outer
# districts; still one API call per type (cost does not scale with radius).
KRAKOW_FULL_RADIUS_M = 17_500

# Default: ul. Wrocławska (Krowodrza / Kleparz), Kraków — tight circle so you
# do not pay for a 15 km disk of results you will then filter. Centre is the
# rough midpoint of OSM ``highway`` segments named Wrocławska in Kraków; radius
# ~550 m covers those segments; widen with ``--radius`` if needed.
WROCLAWSKA_STREET_LAT = 50.07635
WROCLAWSKA_STREET_LNG = 19.92960
WROCLAWSKA_STREET_RADIUS_M = 550

PRESETS: dict[str, tuple[float, float, int]] = {
    "wroclawska": (WROCLAWSKA_STREET_LAT, WROCLAWSKA_STREET_LNG, WROCLAWSKA_STREET_RADIUS_M),
    "city": (KRAKOW_CITY_CENTER_LAT, KRAKOW_CITY_CENTER_LNG, CITY_WIDE_RADIUS_M),
    "krakow": (KRAKOW_CITY_CENTER_LAT, KRAKOW_CITY_CENTER_LNG, KRAKOW_FULL_RADIUS_M),
}
DEFAULT_PRESET = "wroclawska"

# --- Rough cost hints (Places API New, Nearby Search **Enterprise** SKU)
# List prices: https://developers.google.com/maps/billing-and-pricing/pricing#places-pricing
# Nearby Search Enterprise: $35 / 1_000 events (first paid tier after free cap).
# Many accounts also get a $200/month Maps Platform credit — check Cloud Billing.
NEARBY_ENTERPRISE_LIST_USD_PER_1000 = 35.0
NEARBY_ENTERPRISE_FREE_CAP_MONTHLY = 1_000  # billable events at $0 before first tier

# Google Places "type" identifiers for searchNearby (Table A — exact strings).
# Mirrors ``places.yaml`` business categories (Polish comments = human label only).
#
#   bar → bar | lounge cygarowy → lounge_bar | pub → pub | siłownia → gym
#   zegarmistrz → jewelry_store | restauracja → restaurant | kawiarnia → cafe
#   klub nocny → night_club | piekarnia → bakery | cukiernia → confectionery
#   apteka → pharmacy | fryzjer → hair_salon | salon kosmetyczny → beauty_salon
#   stomatolog → dentist | weterynarz → veterinary_care | hotel → hotel
#   mechanik samochodowy → car_repair | kwiaciarnia → florist
#   sklep zoologiczny → pet_store | sklep rowerowy → bicycle_store
#   księgarnia → book_store | pralnia → laundry | zakład pogrzebowy → funeral_home
#   salon tatuażu → body_art_service | sklep z winem → liquor_store
#
# ``store`` stays as a broad retail catch-all (useful beyond the YAML list).
# https://developers.google.com/maps/documentation/places/web-service/place-types
DEFAULT_TYPES: tuple[str, ...] = (
    "bakery",
    "bar",
    "beauty_salon",
    "bicycle_store",
    "body_art_service",
    "book_store",
    "cafe",
    "car_repair",
    "confectionery",
    "dentist",
    "florist",
    "funeral_home",
    "gym",
    "hair_salon",
    "hotel",
    "jewelry_store",
    "laundry",
    "liquor_store",
    "lounge_bar",
    "night_club",
    "pet_store",
    "pharmacy",
    "pub",
    "restaurant",
    "store",
    "veterinary_care",
)

API_ROOT = "https://places.googleapis.com/v1"
NEARBY_URL = f"{API_ROOT}/places:searchNearby"
# Place Details: GET /v1/places/{place_id}, format string filled at call time.
DETAILS_URL_TPL = f"{API_ROOT}/places/{{place_id}}"

# All fields below sit in the Enterprise SKU tier (cheaper than Enterprise +
# Atmosphere). We deliberately avoid ``reviews`` / ``editorialSummary`` etc.
# Format: comma-separated, no spaces, prefixed with ``places.`` for Nearby.
NEARBY_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.types",
        "places.googleMapsUri",
        "places.businessStatus",
    ]
)

# Place Details uses unprefixed field names (the response is a single Place).
DETAILS_FIELD_MASK = ",".join(
    [
        "id",
        "displayName",
        "formattedAddress",
        "nationalPhoneNumber",
        "websiteUri",
        "rating",
        "userRatingCount",
        "types",
        "googleMapsUri",
        "businessStatus",
    ]
)

# Pacing.
REQUEST_DELAY_S = 0.2          # between any two API requests
RETRY_BACKOFF_S = (1.5, 4.0, 9.0)  # back-off when transient errors are returned

# Locale hints – Kraków is in Poland; this gives us Polish display names where
# Google has them, with English fall-backs otherwise.
LANGUAGE_CODE = "pl"
REGION_CODE = "PL"

# Nearby Search (New) caps results at 20 per request and does NOT paginate.
MAX_RESULT_COUNT_HARD_CAP = 20

OUTPUT_CSV = "krakow_leads.csv"

API_KEY_ENV = "GOOGLE_PLACES_API_KEY"

CSV_COLUMNS = [
    "name",
    "area",
    "address",
    "phone",
    "rating",
    "reviews",
    "types",
    "website",
    "score",
    "google_maps_link",
    "place_id",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("places-api-scraper")


def estimate_nearby_list_cost_usd(n_calls: int) -> float:
    """
    Public **list** price for ``n_calls`` Nearby Search (Enterprise) requests,
    ignoring the monthly free cap and any Maps Platform credit — useful as an
    upper bound when budgeting.
    """
    return n_calls * (NEARBY_ENTERPRISE_LIST_USD_PER_1000 / 1000.0)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Lead:
    name: str = ""
    area: str = ""
    address: str = ""
    phone: str = ""
    rating: float | None = None
    reviews: int | None = None
    types: str = ""
    website: str = ""
    score: int = 0
    google_maps_link: str = ""
    place_id: str = field(default="")


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def _headers(api_key: str, field_mask: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": field_mask,
    }


def _request_json(
    session: requests.Session,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict | None = None,
    label: str,
) -> dict | None:
    """
    Issue a request with retry on transient errors (HTTP 429 / 5xx).

    Returns the parsed JSON body on success, ``None`` on permanent 404
    (place no longer exists), and raises ``RuntimeError`` on other 4xx
    responses (REQUEST_DENIED, INVALID_ARGUMENT, etc.).
    """
    last_exc: Exception | None = None
    for attempt, sleep_for in enumerate((0.0, *RETRY_BACKOFF_S)):
        if sleep_for:
            log.warning("%s: retrying after %.1fs (attempt %d)", label, sleep_for, attempt)
            time.sleep(sleep_for)
        try:
            resp = session.request(
                method,
                url,
                headers=headers,
                json=body,
                timeout=20,
            )
        except requests.RequestException as exc:
            last_exc = exc
            continue

        # 404 = place is gone; not retryable, not fatal.
        if resp.status_code == 404:
            return None

        # 429 / 5xx => transient, retry.
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last_exc = RuntimeError(
                f"{label}: transient HTTP {resp.status_code} – "
                f"{(resp.text or '')[:200]}"
            )
            continue

        # 4xx (except 404 / 429) => permanent. Surface the API error message.
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{label}: HTTP {resp.status_code} – {(resp.text or '')[:400]}"
            )

        try:
            return resp.json()
        except ValueError as exc:
            last_exc = exc
            continue

    raise RuntimeError(f"{label}: exhausted retries ({last_exc})")


# ---------------------------------------------------------------------------
# Public API: Nearby Search (New)
# ---------------------------------------------------------------------------

def nearby_search(
    api_key: str,
    *,
    lat: float,
    lng: float,
    radius: int,
    type_: str,
    max_result_count: int = MAX_RESULT_COUNT_HARD_CAP,
    session: requests.Session | None = None,
) -> list[dict]:
    """
    Single Nearby Search (New) call.

    Returns the list of places from ``response.places`` (possibly empty). The
    new API does not paginate Nearby Search – ``maxResultCount`` is capped at
    20 by Google.
    """
    sess = session or requests.Session()
    body = {
        "includedTypes": [type_],
        "maxResultCount": max(1, min(max_result_count, MAX_RESULT_COUNT_HARD_CAP)),
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius),
            },
        },
        "languageCode": LANGUAGE_CODE,
        "regionCode": REGION_CODE,
    }
    data = _request_json(
        sess,
        method="POST",
        url=NEARBY_URL,
        headers=_headers(api_key, NEARBY_FIELD_MASK),
        body=body,
        label=f"nearby[{type_}]",
    )
    if not data:
        return []
    return data.get("places", []) or []


# ---------------------------------------------------------------------------
# Public API: Place Details (New)
# ---------------------------------------------------------------------------

def get_place_details(
    api_key: str,
    place_id: str,
    *,
    session: requests.Session | None = None,
) -> dict | None:
    """
    Place Details (New). Returns the place dict, or ``None`` if not found.

    NOT used by the default flow because Nearby Search already returns every
    field we write to the CSV. Kept as a documented helper so callers can
    enrich an individual record (e.g. to backfill a missing field).
    """
    sess = session or requests.Session()
    return _request_json(
        sess,
        method="GET",
        url=DETAILS_URL_TPL.format(place_id=place_id),
        headers=_headers(api_key, DETAILS_FIELD_MASK),
        label=f"details[{place_id[:10]}…]",
    )


# ---------------------------------------------------------------------------
# Lead scoring + helpers
# ---------------------------------------------------------------------------

# Hostnames treated as “social only” for ``websiteUri`` (no extra API cost —
# same field mask as before; classification is local).
_SOCIAL_WEBSITE_ROOTS: frozenset[str] = frozenset(
    {
        "facebook.com",
        "fb.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "linktr.ee",
        "linktree.com",
        "tiktok.com",
    }
)


def _website_hostname(uri: str) -> str:
    u = uri.strip()
    if not u:
        return ""
    if "://" not in u:
        u = f"https://{u}"
    try:
        host = (urlparse(u).hostname or "").lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_social_media_website_uri(uri: str) -> bool:
    """True if URL host is Facebook, Instagram, X/Twitter, Linktree, or TikTok."""
    h = _website_hostname(uri)
    if not h:
        return False
    return any(h == root or h.endswith("." + root) for root in _SOCIAL_WEBSITE_ROOTS)


def _has_standalone_website(place: dict) -> bool:
    """
    True when ``websiteUri`` is a non-empty URL that is **not** only a social
    profile (own domain / landing page → exclude from leads).
    """
    site = place.get("websiteUri")
    if not site or not str(site).strip():
        return False
    return not _is_social_media_website_uri(str(site).strip())


def score_lead(place: dict) -> int:
    """
    Spec scoring rules:
        +5 if no standalone website (empty or social-only URL)
        +3 if rating >= 4.2
        +3 if reviews >= 30
        +2 if reviews >= 100
    """
    score = 0
    if not _has_standalone_website(place):
        score += 5
    rating = place.get("rating")
    if isinstance(rating, (int, float)) and rating >= 4.2:
        score += 3
    reviews = place.get("userRatingCount") or 0
    if reviews >= 30:
        score += 3
    if reviews >= 100:
        score += 2
    return score


def google_maps_link(place_id: str, place: dict | None = None) -> str:
    """
    Prefer the API-provided ``googleMapsUri`` when present; otherwise build
    the canonical legacy deep-link from the place_id.
    """
    if place:
        api_uri = place.get("googleMapsUri")
        if api_uri:
            return api_uri
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


def _display_name(place: dict) -> str:
    """``displayName`` is ``{"text": "...", "languageCode": "..."}`` in v1."""
    raw = place.get("displayName")
    if isinstance(raw, dict):
        return (raw.get("text") or "").strip()
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _is_kebab_name(place: dict) -> bool:
    """True if the venue name should be excluded (kebab / kebap spelling)."""
    name = _display_name(place).lower()
    return "kebab" in name or "kebap" in name


def _is_temporarily_closed(place: dict) -> bool:
    """
    True when Google reports ``CLOSED_TEMPORARILY`` (still deduped via ``place_id``).

    If ``businessStatus`` is absent, the place is kept — status unknown.
    """
    return place.get("businessStatus") == "CLOSED_TEMPORARILY"


# ---------------------------------------------------------------------------
# Multi-cell YAML (Kraków tiling)
# ---------------------------------------------------------------------------

def load_search_cells(path: Path) -> list[tuple[str, str, float, float, int]]:
    """
    Load ``cells`` from YAML: each row needs ``id``, ``lat``, ``lng``, ``radius_m``.
    Optional ``name`` is a human-readable area label for the CSV; defaults to ``id``
    with underscores replaced by spaces.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "cells" not in raw:
        raise ValueError(f"{path}: expected a mapping with key 'cells'")
    rows = raw["cells"]
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{path}: 'cells' must be a non-empty list")
    out: list[tuple[str, str, float, float, int]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if "id" not in row or "lat" not in row or "lng" not in row or "radius_m" not in row:
            raise ValueError(f"{path}: cells[{i}] needs id, lat, lng, radius_m")
        cid = str(row["id"]).strip()
        label = row.get("name")
        if isinstance(label, str) and label.strip():
            area_name = label.strip()
        else:
            area_name = cid.replace("_", " ")
        out.append(
            (
                cid,
                area_name,
                float(row["lat"]),
                float(row["lng"]),
                int(row["radius_m"]),
            )
        )
    if not out:
        raise ValueError(f"{path}: no valid cells after parsing")
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _merge_area_label(existing: str, new: str) -> str:
    """Append ``new`` to semicolon-separated area list if not already present."""
    parts = [p.strip() for p in existing.split(";") if p.strip()] if existing else []
    n = new.strip()
    if n and n not in parts:
        parts.append(n)
    return "; ".join(parts)


def _collect_leads_into(
    api_key: str,
    session: requests.Session,
    lat: float,
    lng: float,
    radius: int,
    types: Iterable[str],
    max_result_count: int,
    seen_place_ids: set[str],
    leads: list[Lead],
    *,
    area_name: str = "",
    cell_label: str = "",
    leads_by_place_id: dict[str, Lead] | None = None,
) -> None:
    """Run Nearby Search for each type at one circle; append new leads (no standalone site)."""
    prefix = f"[{cell_label}] " if cell_label else ""
    type_list = list(types)
    for type_ in type_list:
        log.info(
            "%sSearching type=%s within %d m of (%.4f, %.4f)",
            prefix,
            type_,
            radius,
            lat,
            lng,
        )
        try:
            places = nearby_search(
                api_key,
                lat=lat,
                lng=lng,
                radius=radius,
                type_=type_,
                max_result_count=max_result_count,
                session=session,
            )
        except RuntimeError as exc:
            log.error("%sNearby search for %s failed: %s", prefix, type_, exc)
            continue

        log.info("%s  type=%s → %d results", prefix, type_, len(places))

        for place in places:
            place_id = place.get("id") or ""
            if not place_id:
                continue
            if place_id in seen_place_ids:
                if leads_by_place_id is not None and place_id in leads_by_place_id:
                    ex = leads_by_place_id[place_id]
                    ex.area = _merge_area_label(ex.area, area_name)
                continue
            seen_place_ids.add(place_id)

            if _has_standalone_website(place):
                continue

            if _is_temporarily_closed(place):
                continue

            if _is_kebab_name(place):
                continue

            site_raw = (place.get("websiteUri") or "").strip()
            website_col = site_raw if site_raw else ""

            lead = Lead(
                name=_display_name(place),
                area=area_name,
                address=(place.get("formattedAddress") or "").strip(),
                phone=(place.get("nationalPhoneNumber") or "").strip(),
                rating=place.get("rating"),
                reviews=place.get("userRatingCount"),
                types="|".join(place.get("types") or []),
                website=website_col,
                score=score_lead(place),
                google_maps_link=google_maps_link(place_id, place),
                place_id=place_id,
            )
            leads.append(lead)
            if leads_by_place_id is not None:
                leads_by_place_id[place_id] = lead
            log.info(
                "%s  + %s (rating=%s reviews=%s score=%d)",
                prefix,
                lead.name,
                lead.rating,
                lead.reviews,
                lead.score,
            )

        time.sleep(REQUEST_DELAY_S)


def collect_leads_all_cells(
    api_key: str,
    cells: list[tuple[str, str, float, float, int]],
    types: Iterable[str],
    *,
    max_result_count: int = MAX_RESULT_COUNT_HARD_CAP,
) -> list[Lead]:
    """
    Run every search cell sequentially with **global** ``place_id`` deduplication.
    """
    session = requests.Session()
    seen: set[str] = set()
    leads: list[Lead] = []
    by_id: dict[str, Lead] = {}
    for cell_id, area_name, lat, lng, radius in cells:
        log.info("=== Cell %r — %r (%.5f, %.5f) r=%dm ===", cell_id, area_name, lat, lng, radius)
        _collect_leads_into(
            api_key,
            session,
            lat,
            lng,
            radius,
            types,
            max_result_count,
            seen,
            leads,
            area_name=area_name,
            cell_label=cell_id,
            leads_by_place_id=by_id,
        )
    return leads


def collect_leads(
    api_key: str,
    *,
    lat: float,
    lng: float,
    radius: int,
    types: Iterable[str],
    max_result_count: int = MAX_RESULT_COUNT_HARD_CAP,
    area_name: str = "",
) -> list[Lead]:
    """
    Run Nearby Search (New) for every ``type``, filter to places without a
    standalone website (social URLs kept), deduplicate by place_id, and return
    scored leads.

    No Place Details calls are made: the Nearby field mask already returns every
    field we write to the CSV.
    """
    session = requests.Session()
    seen: set[str] = set()
    leads: list[Lead] = []
    _collect_leads_into(
        api_key,
        session,
        lat,
        lng,
        radius,
        types,
        max_result_count,
        seen,
        leads,
        area_name=area_name,
    )
    return leads


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(leads: list[Lead], path: str) -> None:
    """Write leads to ``path`` with the spec column order, sorted by score."""
    if not leads:
        log.warning("No leads collected – writing empty CSV with header only.")
        pd.DataFrame(columns=CSV_COLUMNS).to_csv(path, index=False)
        return

    df = pd.DataFrame(asdict(lead) for lead in leads)
    df = df[CSV_COLUMNS].sort_values("score", ascending=False, kind="stable")
    df.to_csv(path, index=False)
    log.info("Wrote %d leads to %s", len(df), path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Google Places API (New) lead scraper for Kraków, Poland."
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_CSV,
        help="CSV output path (default: %(default)s).",
    )
    parser.add_argument(
        "--preset",
        choices=tuple(PRESETS.keys()),
        default=DEFAULT_PRESET,
        metavar="NAME",
        help=(
            "Geographic preset: 'wroclawska' — tight circle on ul. Wrocławska (default); "
            "'city' — 15 km from Rynek; 'krakow' — ~17.5 km from Rynek (wider whole-city disk)."
        ),
    )
    parser.add_argument(
        "--lat",
        type=float,
        default=None,
        help="Override preset centre latitude.",
    )
    parser.add_argument(
        "--lng",
        type=float,
        default=None,
        help="Override preset centre longitude.",
    )
    parser.add_argument(
        "--radius",
        type=int,
        default=None,
        help="Override preset search radius in metres.",
    )
    parser.add_argument(
        "--types",
        nargs="+",
        default=list(DEFAULT_TYPES),
        metavar="TYPE",
        help="Google Places type identifiers (default: the spec sweep).",
    )
    parser.add_argument(
        "--cells-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "YAML with key 'cells': list of {id, lat, lng, radius_m}. Runs every "
            "cell sequentially with global place_id dedupe (max coverage in Kraków). "
            "Ignores --preset, --lat, --lng, --radius. Example: krakow_search_cells.yaml"
        ),
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=MAX_RESULT_COUNT_HARD_CAP,
        help=(
            "Max places per Nearby Search request "
            f"(Google cap = {MAX_RESULT_COUNT_HARD_CAP}; default: %(default)s)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        log.error(
            "Missing API key. Set %s in your environment, e.g.:\n"
            "    export %s=AIza...",
            API_KEY_ENV, API_KEY_ENV,
        )
        return 2

    base_lat, base_lng, base_radius = PRESETS[args.preset]
    lat = args.lat if args.lat is not None else base_lat
    lng = args.lng if args.lng is not None else base_lng
    radius = args.radius if args.radius is not None else base_radius

    n_types = len(args.types)

    if args.cells_file is not None:
        cells_path = args.cells_file.expanduser().resolve()
        if not cells_path.is_file():
            log.error("Cells file not found: %s", cells_path)
            return 1
        try:
            cells = load_search_cells(cells_path)
        except (ValueError, OSError, yaml.YAMLError) as exc:
            log.error("Invalid cells file: %s", exc)
            return 1
        n_cells = len(cells)
        n_calls = n_types * n_cells
        log.info(
            "Multi-cell mode: %d cells × %d types = %d Nearby Search calls "
            "(zero Place Details).",
            n_cells,
            n_types,
            n_calls,
        )
        log.info("Cells file: %s", cells_path)
        log.info("Types (%d): %s", n_types, ",".join(args.types))
        list_usd = estimate_nearby_list_cost_usd(n_calls)
        log.info(
            "Cost hint (Nearby Search Enterprise list price): ~$%.2f USD for %d call(s) "
            "(@ $%.0f/1000 in the first paid tier). Often $0 in practice: %d free/mo/SKU + "
            "$200 Maps credit on many accounts. "
            "https://developers.google.com/maps/billing-and-pricing/pricing#places-pricing",
            list_usd,
            n_calls,
            NEARBY_ENTERPRISE_LIST_USD_PER_1000,
            NEARBY_ENTERPRISE_FREE_CAP_MONTHLY,
        )
        try:
            leads = collect_leads_all_cells(
                api_key,
                cells,
                args.types,
                max_result_count=args.max_results,
            )
        except KeyboardInterrupt:
            log.warning("Interrupted by user – partial CSV may be incomplete.")
            return 130
    else:
        log.info(
            "Billable Nearby Search calls this run: %d (one per type; zero Place Details).",
            n_types,
        )
        log.info(
            "Area: preset=%s → centre (%.5f, %.5f) radius=%dm | max_results/type=%d",
            args.preset,
            lat,
            lng,
            radius,
            args.max_results,
        )
        log.info("Types (%d): %s", n_types, ",".join(args.types))
        list_usd = estimate_nearby_list_cost_usd(n_types)
        log.info(
            "Cost hint (Nearby Search Enterprise list price): ~$%.2f USD for %d call(s) "
            "(@ $%.0f/1000 in the first paid tier). Often $0 in practice: %d free/mo/SKU + "
            "$200 Maps credit on many accounts. "
            "https://developers.google.com/maps/billing-and-pricing/pricing#places-pricing",
            list_usd,
            n_types,
            NEARBY_ENTERPRISE_LIST_USD_PER_1000,
            NEARBY_ENTERPRISE_FREE_CAP_MONTHLY,
        )
        try:
            leads = collect_leads(
                api_key,
                lat=lat,
                lng=lng,
                radius=radius,
                types=args.types,
                max_result_count=args.max_results,
                area_name=str(args.preset).replace("_", " "),
            )
        except KeyboardInterrupt:
            log.warning("Interrupted by user – partial CSV may be incomplete.")
            return 130

    export_csv(leads, args.output)
    log.info(
        "Done. %d leads (no standalone site) across %d type(s).",
        len(leads),
        len(args.types),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
