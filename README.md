# places-scraper

Lead-generation scraper for Google Maps. Finds businesses in Kraków, Poland
that **do not have a website**, scores them as leads and exports the result to
`krakow_scraped_leads.csv`.

No official Google API is used – the scraper drives a real Chromium instance
with [Playwright](https://playwright.dev/python/).

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
