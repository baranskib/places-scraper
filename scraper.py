"""
Google Maps lead-generation scraper.

Scrapes business listings from Google Maps for several Kraków-based queries,
keeps only those WITHOUT a website, scores them as leads and writes the
result to ``krakow_scraped_leads.csv``.

The scraper uses Playwright (sync API) and intentionally relies on stable
ARIA roles and ``data-item-id`` attributes rather than fragile, hashed
class names, so it survives most cosmetic Google Maps redesigns.

Run:
    python scraper.py                 # headless; queries = places.yaml × districts.yaml
    python scraper.py --debug         # visible browser, slower, useful when
                                      # selectors stop matching
    python scraper.py --queries "bar Krowodrza"  # override with explicit strings

Places and districts are read from YAML (recommended), JSON, or plain text — see
``--places-file`` / ``--districts-file``.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

import pandas as pd
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def maps_search_url(query: str) -> str:
    """Deep-link into Maps search results (more reliable than the omnibox)."""
    return f"https://www.google.com/maps/search/{quote_plus(query)}?hl=pl"

PLACES_FILE = "places.yaml"
DISTRICTS_FILE = "districts.yaml"

OUTPUT_CSV = "krakow_scraped_leads.csv"

# ``div[role="main"]`` matches both the results list and the place drawer; the first
# ``h1`` is often a label like "Wyniki" / "Results", not a business name.
SPURIOUS_PLACE_TITLES: frozenset[str] = frozenset(
    {
        "wyniki",
        "results",
        "search results",
        "wyszukiwanie",
        "szukaj",
        "filtry",
        "filters",
        "mapa",
        "map",
    }
)

# Hard caps to keep the run bounded even if Google keeps streaming results.
MAX_RESULTS_PER_QUERY = 120
MAX_SCROLLS_WITHOUT_GROWTH = 4

# A realistic UA string. Default headless UA is a known fingerprint.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("places-scraper")


def _strings_from_sequence(data: object, path: Path) -> list[str]:
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list, got {type(data).__name__}")
    out: list[str] = []
    for item in data:
        if not isinstance(item, str) or not item.strip():
            continue
        out.append(item.strip())
    return out


def _load_line_list_file(text: str) -> list[str]:
    """One entry per non-empty line; lines starting with # are ignored."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def load_string_list(path: Path) -> list[str]:
    """
    Load a list of strings from ``path``.

    * ``.yaml`` / ``.yml`` — YAML sequence (supports ``#`` comments).
    * ``.json`` — JSON array of strings.
    * Anything else — plain text, one item per line (``#`` starts a comment line).
    """
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
        if data is None:
            return []
        return _strings_from_sequence(data, path)
    if suffix == ".json":
        data = json.loads(text)
        return _strings_from_sequence(data, path)
    return _load_line_list_file(text)


def queries_from_places_and_districts(
    places_path: Path, districts_path: Path
) -> list[str]:
    """Cartesian product: ``{place} {district}`` for each pair (district × place)."""
    places = load_string_list(places_path)
    districts = load_string_list(districts_path)
    if not places:
        raise ValueError(f"{places_path}: no non-empty place strings")
    if not districts:
        raise ValueError(f"{districts_path}: no non-empty district strings")
    return [f"{place} {district}" for district in districts for place in places]


def place_details_panel_locator(page: Page) -> Locator:
    """
    The summary panel for one POI (address / phone / category rows).

    Maps renders multiple ``role=main`` regions; the list sidebar often exposes an
    ``h1`` such as "Wyniki" which must not be treated as the place name.
    """
    mains = page.locator('div[role="main"]')
    for hint in (
        'button[data-item-id="address"]',
        'button[data-item-id^="phone:tel:"]',
        'button[jsaction*="category"]',
        'a[data-item-id="authority"]',
    ):
        panel = mains.filter(has=page.locator(hint))
        if panel.count() > 0:
            return panel.first
    if mains.count() > 1:
        return mains.last
    return mains.first


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Business:
    name: str = ""
    address: str = ""
    phone: str = ""
    rating: float | None = None
    reviews: int | None = None
    category: str = ""
    website: str = ""
    score: int = 0
    query: str = field(default="", repr=False)  # kept for debugging only

    def dedup_key(self) -> tuple[str, str]:
        """Identity used for deduplication across queries."""
        return (self.name.strip().lower(), self.address.strip().lower())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human_delay(min_s: float = 1.6, max_s: float = 5.6) -> None:
    """Sleep a random short period to look less bot-like."""
    time.sleep(random.uniform(min_s, max_s))


def parse_rating(text: str) -> float | None:
    """Pull a float rating out of strings like '4,3' / '4.3 stars'."""
    if not text:
        return None
    match = re.search(r"(\d+[.,]\d+)", text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_review_count(text: str) -> int | None:
    """Pull an int review count out of strings like '(1,234)' / '1.2k reviews'."""
    if not text:
        return None
    cleaned = text.replace("\xa0", " ")
    # Handle "1.2k" / "2,3 tys."  -> shorthand used in some locales.
    short = re.search(r"(\d+(?:[.,]\d+)?)\s*[kK]", cleaned)
    if short:
        try:
            return int(float(short.group(1).replace(",", ".")) * 1000)
        except ValueError:
            pass
    digits = re.sub(r"[^\d]", "", cleaned)
    return int(digits) if digits else None


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class GoogleMapsScraper:
    """Headless Google Maps scraper for lead generation."""

    def __init__(self, headless: bool = True, slow_mo_ms: int = 0) -> None:
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

        # Deduplication state shared across all queries.
        self._seen: set[tuple[str, str]] = set()
        self.results: list[Business] = []

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "GoogleMapsScraper":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo_ms,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            user_agent=USER_AGENT,
            locale="pl-PL",
            viewport={"width": 1400, "height": 900},
        )
        self.page = self._context.new_page()
        self.page.set_default_timeout(15_000)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw is not None:
            self._pw.stop()

    # -- public API --------------------------------------------------------

    def run(self, queries: Iterable[str]) -> list[Business]:
        """Run every query end-to-end and return the collected leads."""
        assert self.page is not None, "Use the scraper as a context manager."

        query_list = list(queries)
        for query in query_list:
            try:
                self.search(query)
                self.scroll_results()
                cards = self.extract_listings()
                log.info("[%s] %d cards visible after scrolling", query, len(cards))
                self._process_cards(cards, query)
            except Exception as exc:  # noqa: BLE001
                log.exception("Query failed: %s (%s)", query, exc)
                continue
        return self.results

    # -- navigation --------------------------------------------------------

    def _dismiss_consent(self) -> None:
        """Click the EU cookie/consent dialog if Google shows one."""
        assert self.page is not None
        # Try a handful of language-agnostic buttons.
        labels = (
            "Accept all",
            "I agree",
            "Reject all",
            "Zaakceptuj wszystko",
            "Zaakceptuj wszystkie",
            "Odrzuć wszystko",
        )
        # Consent often lives in an iframe (e.g. consent.google.com); scan every frame.
        for frame in self.page.frames:
            for label in labels:
                try:
                    btn = frame.get_by_role(
                        "button", name=re.compile(label, re.I)
                    )
                    if btn.count() > 0:
                        btn.first.click(timeout=3_000)
                        human_delay(0.4, 0.9)
                        return
                except PlaywrightTimeoutError:
                    continue
                except Exception:  # noqa: BLE001
                    continue

    # -- step 1: search ----------------------------------------------------

    def search(self, query: str) -> None:
        """Open results for ``query`` via Maps URL (avoids brittle omnibox selectors)."""
        assert self.page is not None
        log.info("Searching: %s", query)

        self.page.goto(maps_search_url(query), wait_until="load", timeout=60_000)
        self._dismiss_consent()
        human_delay(0.25, 0.55)
        self._dismiss_consent()

        try:
            self.page.wait_for_selector(
                'div[role="feed"], div[role="main"]', timeout=30_000
            )
        except PlaywrightTimeoutError:
            log.warning("No results panel appeared for %r", query)
        human_delay(1.0, 2.0)

    # -- step 2: scroll the results panel ---------------------------------

    def scroll_results(self) -> None:
        """Scroll the results sidebar until Google stops returning new items."""
        assert self.page is not None

        feed = self.page.locator('div[role="feed"]')
        if feed.count() == 0:
            # Single-place result page (no list) – nothing to scroll.
            return

        feed_handle = feed.first
        previous_count = 0
        stagnant_rounds = 0

        for scroll_idx in range(40):  # absolute upper bound
            cards = feed_handle.locator("div[jsaction] > a.hfpxzc")
            count = cards.count()

            if count >= MAX_RESULTS_PER_QUERY:
                log.debug("Hit MAX_RESULTS_PER_QUERY (%d)", MAX_RESULTS_PER_QUERY)
                break

            if count == previous_count:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            previous_count = count

            # End-of-list sentinel that Maps appends when no more results exist.
            end_marker = self.page.locator(
                'div[role="feed"] >> text=/You\\u2019ve reached the end of the list/i'
            )
            if end_marker.count() > 0 or stagnant_rounds >= MAX_SCROLLS_WITHOUT_GROWTH:
                log.debug("End of results detected after %d scrolls", scroll_idx)
                break

            # Scroll the *feed* element itself; scrolling window has no effect.
            feed_handle.evaluate("el => el.scrollBy(0, el.scrollHeight)")
            human_delay(1.0, 2.2)

    # -- step 3: collect listing cards ------------------------------------

    def extract_listings(self) -> list[Locator]:
        """Return the per-result anchor locators currently in the sidebar."""
        assert self.page is not None
        feed = self.page.locator('div[role="feed"]')
        if feed.count() == 0:
            # Single-result navigation: synthesize a list with the main card.
            return [self.page.locator('div[role="main"]').first]
        anchors = feed.first.locator("a.hfpxzc")
        # `all()` snapshots the locators so they survive subsequent re-renders.
        return anchors.all()[:MAX_RESULTS_PER_QUERY]

    # -- step 4: drill into each card --------------------------------------

    def _process_cards(self, cards: list[Locator], query: str) -> None:
        """Click every card, extract details, filter and store."""
        assert self.page is not None
        for idx, card in enumerate(cards, start=1):
            for attempt in (1, 2):  # one retry for transient failures
                try:
                    card.scroll_into_view_if_needed(timeout=3_000)
                    card.click(timeout=5_000)
                    self._wait_for_details_panel()
                    business = self.extract_details(query)
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "Card %d attempt %d failed: %s", idx, attempt, exc
                    )
                    human_delay(0.8, 1.5)
                    business = None
            else:
                continue

            if business is None or not business.name:
                continue
            if self._is_noise_lead(business):
                log.debug("Skipping UI/chrome row: %r", business.name)
                continue
            if business.website:
                # Filter requirement: only leads WITHOUT a website.
                continue
            if business.dedup_key() in self._seen:
                continue

            business.score = self.score_lead(business)
            self._seen.add(business.dedup_key())
            self.results.append(business)
            log.info(
                "  + %s (rating=%s reviews=%s score=%d)",
                business.name,
                business.rating,
                business.reviews,
                business.score,
            )
            human_delay(0.5, 1.2)

    @staticmethod
    def _is_noise_lead(business: Business) -> bool:
        """Drop headings like "Wyniki" and other non-POI titles."""
        title = business.name.strip().lower()
        return title in SPURIOUS_PLACE_TITLES

    def _wait_for_details_panel(self) -> None:
        """Wait until the place summary panel has rendered the title."""
        assert self.page is not None
        try:
            self.page.wait_for_selector(
                'div[role="main"]:has(button[data-item-id="address"]) h1, '
                'div[role="main"]:has(button[data-item-id^="phone:tel:"]) h1, '
                'div[role="main"]:has(button[jsaction*="category"]) h1, '
                'div[role="main"]:has(a[data-item-id="authority"]) h1',
                timeout=12_000,
            )
        except PlaywrightTimeoutError:
            log.debug(
                "POI panel hints slow or missing; falling back to first main h1"
            )
            self.page.wait_for_selector('div[role="main"] h1', timeout=5_000)
        human_delay(0.4, 0.9)

    # -- step 5: extract one business -------------------------------------

    def extract_details(self, query: str) -> Business:
        """Read all fields from the open details panel."""
        assert self.page is not None
        panel = place_details_panel_locator(self.page)
        biz = Business(query=query)

        biz.name = self._safe_text(panel.locator("h1").first)

        # Address – the data-item-id is stable and locale-independent.
        biz.address = self._safe_attr_text(
            panel.locator('button[data-item-id="address"]')
        )

        # Phone – data-item-id always starts with "phone:tel:".
        biz.phone = self._safe_attr_text(
            panel.locator('button[data-item-id^="phone:tel:"]')
        )

        # Website – the canonical "authority" link. Empty string == no website.
        website_link = panel.locator('a[data-item-id="authority"]')
        if website_link.count() > 0:
            try:
                biz.website = website_link.first.get_attribute("href") or ""
            except Exception:  # noqa: BLE001
                biz.website = ""

        # Rating + review count – the rating widget has aria-label like
        # "4.3 stars" and a sibling span with "(1,234)".
        rating_widget = panel.locator('div.F7nice, div[aria-label*="stars" i]').first
        if rating_widget.count() > 0:
            aria = rating_widget.get_attribute("aria-label") or ""
            biz.rating = parse_rating(aria) or parse_rating(
                self._safe_text(rating_widget)
            )
        # Reviews count – usually the parenthesised number near the rating.
        reviews_loc = panel.locator(
            'button[aria-label*="review" i], span[aria-label*="review" i]'
        )
        if reviews_loc.count() > 0:
            biz.reviews = parse_review_count(
                reviews_loc.first.get_attribute("aria-label") or ""
            )

        # Category – the small button right under the title; jsaction contains
        # "category" and the text is plain ("Restaurant", "Dentist", ...).
        category_loc = panel.locator('button[jsaction*="category"]')
        if category_loc.count() > 0:
            biz.category = self._safe_text(category_loc.first)

        return biz

    # -- step 6: score the lead -------------------------------------------

    @staticmethod
    def score_lead(business: Business) -> int:
        """Apply the scoring rules from the spec."""
        score = 0
        if not business.website:
            score += 5
        if business.rating is not None and business.rating >= 4.2:
            score += 3
        if business.reviews is not None:
            if business.reviews >= 30:
                score += 3
            if business.reviews >= 100:
                score += 2
        return score

    # -- low-level helpers -------------------------------------------------

    @staticmethod
    def _safe_text(locator: Locator) -> str:
        try:
            if locator.count() == 0:
                return ""
            return (locator.first.inner_text(timeout=2_000) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _safe_attr_text(locator: Locator) -> str:
        """Address/phone buttons store the visible value in aria-label."""
        try:
            if locator.count() == 0:
                return ""
            label = locator.first.get_attribute("aria-label") or ""
            # aria-label looks like "Address: ul. Floriańska 1" – strip prefix.
            return re.sub(r"^[^:]+:\s*", "", label).strip()
        except Exception:  # noqa: BLE001
            return ""


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "name",
    "address",
    "phone",
    "rating",
    "reviews",
    "category",
    "website",
    "score",
]


def export_csv(businesses: list[Business], path: str) -> None:
    """Write the leads to ``path`` with the required column order."""
    if not businesses:
        log.warning("No leads collected – writing an empty CSV")
        pd.DataFrame(columns=CSV_COLUMNS).to_csv(path, index=False)
        return

    df = pd.DataFrame(asdict(b) for b in businesses)
    df = df[CSV_COLUMNS].sort_values("score", ascending=False)
    df.to_csv(path, index=False)
    log.info("Wrote %d leads to %s", len(df), path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run with a visible browser window and slower actions.",
    )
    parser.add_argument(
        "--output",
        default=OUTPUT_CSV,
        help="Path to the CSV output file (default: %(default)s).",
    )
    parser.add_argument(
        "--queries",
        nargs="+",
        default=None,
        metavar="Q",
        help=(
            "Explicit search strings. If omitted, queries are built from "
            "--places-file × --districts-file (each place paired with each district)."
        ),
    )
    parser.add_argument(
        "--places-file",
        type=Path,
        default=Path(PLACES_FILE),
        metavar="PATH",
        help=(
            "YAML list, JSON array, or plain-text lines of place keywords "
            "(.yaml / .json / other). Default: %(default)s."
        ),
    )
    parser.add_argument(
        "--districts-file",
        type=Path,
        default=Path(DISTRICTS_FILE),
        metavar="PATH",
        help=(
            "YAML list, JSON array, or plain-text lines of district names "
            "(.yaml / .json / other). Default: %(default)s."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.queries is not None:
        query_list = list(args.queries)
    else:
        query_list = queries_from_places_and_districts(
            args.places_file,
            args.districts_file,
        )

    log.info(
        "Loaded %d queries (%s)",
        len(query_list),
        "from CLI" if args.queries is not None else "places × districts files",
    )

    with GoogleMapsScraper(
        headless=not args.debug,
        slow_mo_ms=150 if args.debug else 0,
    ) as scraper:
        businesses = scraper.run(query_list)

    export_csv(businesses, args.output)
    log.info(
        "Done. %d leads without a website across %d queries.",
        len(businesses),
        len(query_list),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
