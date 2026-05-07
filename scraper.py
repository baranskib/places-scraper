"""
Google Maps lead-generation scraper.

Scrapes business listings from Google Maps for several Kraków-based queries,
keeps only those WITHOUT a website, scores them as leads and writes the
result to ``krakow_scraped_leads.csv``.

The scraper uses Playwright (sync API) and intentionally relies on stable
ARIA roles and ``data-item-id`` attributes rather than fragile, hashed
class names, so it survives most cosmetic Google Maps redesigns.

Run:
    python scraper.py                 # headless
    python scraper.py --debug         # visible browser, slower, useful when
                                      # selectors stop matching
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
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

GOOGLE_MAPS_URL = "https://www.google.com/maps?hl=en"


def maps_search_url(query: str) -> str:
    """Deep-link into Maps search results (more reliable than the bare homepage)."""
    return f"https://www.google.com/maps/search/{quote_plus(query)}?hl=en"

DEFAULT_QUERIES: tuple[str, ...] = (
    "restaurants Kraków",
    # "bars Kraków",
    # "beauty salon Kraków",
    # "dentist Kraków",
    # "gym Kraków",
)

OUTPUT_CSV = "krakow_scraped_leads.csv"

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
            locale="en-US",
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
        self._open_maps(query_list[0] if query_list else None)
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

    def _open_maps(self, first_query: str | None) -> None:
        """Navigate to Google Maps and dismiss the consent banner if shown."""
        assert self.page is not None
        url = maps_search_url(first_query) if first_query else GOOGLE_MAPS_URL
        self.page.goto(url, wait_until="load", timeout=60_000)
        self._dismiss_consent()
        human_delay(0.3, 0.7)
        self._dismiss_consent()
        self._wait_for_maps_ready()
        human_delay()

    def _wait_for_maps_ready(self) -> None:
        """Wait until the Maps app shell or results UI is visible."""
        assert self.page is not None
        # Omnibox id / ARIA variants, or results feed — whichever appears first.
        ready = self.page.locator(
            "#searchboxinput, "
            'textarea[aria-label*="Search" i], '
            'input[aria-label*="Search" i], '
            'div[role="feed"]'
        ).first
        try:
            ready.wait_for(state="visible", timeout=45_000)
        except PlaywrightTimeoutError:
            log.error(
                "Maps UI did not appear — url=%r title=%r",
                self.page.url,
                self.page.title(),
            )
            raise

    def _omnibox(self) -> Locator:
        """Search field at the top of Maps (id/role vary by release)."""
        assert self.page is not None
        return self.page.locator(
            "#searchboxinput, "
            'textarea[aria-label*="Search" i], '
            'input[aria-label*="Search" i]'
        ).first

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
        """Type ``query`` into the search box and submit."""
        assert self.page is not None
        log.info("Searching: %s", query)

        box = self._omnibox()
        box.click()
        # Clear any previous query before typing the next one.
        box.fill("")
        human_delay(0.2, 0.5)
        box.type(query, delay=random.randint(40, 110))
        human_delay(0.3, 0.7)
        self.page.keyboard.press("Enter")

        # Either a list (role=feed) or a single matched place (role=main) appears.
        try:
            self.page.wait_for_selector(
                'div[role="feed"], div[role="main"]', timeout=20_000
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

    def _wait_for_details_panel(self) -> None:
        """Wait until the right-hand details panel has rendered the title."""
        assert self.page is not None
        # h1.DUwDvf has been the place title element for a long time, but the
        # plain h1 inside role=main is a more resilient fallback.
        self.page.wait_for_selector(
            'div[role="main"] h1', timeout=10_000
        )
        human_delay(0.4, 0.9)

    # -- step 5: extract one business -------------------------------------

    def extract_details(self, query: str) -> Business:
        """Read all fields from the open details panel."""
        assert self.page is not None
        panel = self.page.locator('div[role="main"]').first
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
        default=list(DEFAULT_QUERIES),
        help="Override the search queries.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    with GoogleMapsScraper(
        headless=not args.debug,
        slow_mo_ms=150 if args.debug else 0,
    ) as scraper:
        businesses = scraper.run(args.queries)

    export_csv(businesses, args.output)
    log.info(
        "Done. %d leads without a website across %d queries.",
        len(businesses),
        len(args.queries),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
