# FreeThe10Ks — EDGAR 10‑K Statement Extractor + Local Viewer

Turn bloated 10‑Ks into **clean, navigable financial statements** (Balance Sheet, Income Statement, Cash Flow) and browse them locally with a fast UI.

This repo has two parts:

- **Extractor (`edgar_extract.py`)**: downloads the *as-filed* statement tables from EDGAR report HTML (R*.htm) and exports **CSV + JSON** plus a **manifest** for indexing.
- **Viewer (`edgar_viewer.py`)**: a **local FastAPI** app that auto-discovers multiple company exports and lets you browse filings grouped by year, with collapsible line-item hierarchy and search.

---

## Why this exists

Most 10‑Ks are huge documents where the core financial statements are buried under pages of narrative. This project pulls the statements out into a consistent, structured format and gives you a lightweight way to explore them.

---

## Features

### Extractor
- Pulls filings via `data.sec.gov/submissions`
- Finds the statement reports via `FilingSummary.xml`
- Parses tables from EDGAR’s statement HTML reports (R*.htm)
- Exports:
  - `balance_sheet.csv` + `balance_sheet.json`
  - `income_statement.csv` + `income_statement.json`
  - `cash_flow.csv` + `cash_flow.json`
  - `manifest.json` per company (indexes filings + output paths + source URLs)
  - `FilingSummary.xml` + the referenced `R*.htm` files used for parsing
- Preserves structure using an `indent` array (either from HTML indentation or inferred)

### Viewer
- Auto-discovers exports from a “super-root” directory
- Homepage: shows every discovered company/CIK + latest filing date
- Company page: filings grouped by **report year** (from `reportDate`)
- Statement page:
  - collapsible tree using indentation
  - expand/collapse all
  - fast label filter
  - link back to the original EDGAR report page

---

## Repo layout

Recommended:

```
.
├── edgar_extract.py
├── edgar_viewer.py
├── requirements.txt
└── statements/                    # generated data (optional to commit)
    ├── ui_statements/
    │   └── 0001511737/
    │       ├── manifest.json
    │       └── <accession>/
    │           ├── balance_sheet.json
    │           ├── income_statement.json
    │           └── cash_flow.json
    └── aapl_statements/
        └── 0000320193/
            └── manifest.json
```

The viewer supports these layouts under `EDGAR_OUT_ROOT`:

- `ROOT/<CIK>/manifest.json`
- `ROOT/<collection>/<CIK>/manifest.json`

---

## Installation

### 1) Create a virtual environment

**Windows (PowerShell)**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**macOS/Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2) Install dependencies

```bash
pip install -r requirements.txt
```

Suggested `requirements.txt`:
```txt
requests>=2.31.0
beautifulsoup4>=4.12.0
lxml>=5.0.0
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
```

---

## SEC User-Agent (required)

SEC expects automated access to identify itself with a descriptive User-Agent including contact info.

You can set it as an environment variable:

**Windows (PowerShell)**
```powershell
$env:SEC_UA = "FreeThe10Ks (your_email@example.com)"
```

**macOS/Linux**
```bash
export SEC_UA="FreeThe10Ks (your_email@example.com)"
```

Or pass `--user-agent` on each run.

---

## Extract statements (edgar_extract.py)

### Basic example
```bash
python edgar_extract.py --cik 0001511737 --out statements/ui_statements
```

### Useful options
```bash
python edgar_extract.py --cik 0001511737 `
  --years 8 `
  --limit 8 `
  --out statements/ui_statements `
  --include-amends
```

Flags:
- `--years`: lookback window
- `--limit`: max number of filings to process
- `--include-amends`: include `10-K/A`
- `--keep-abstract`: keep XBRL scaffolding rows (default is to drop them)
- `--min-interval`: rate-limit delay between SEC requests

### Batch extraction (multiple CIKs)

**PowerShell**
```powershell
$env:SEC_UA = "FreeThe10Ks (your_email@example.com)"

$targets = @(
  @{ name = "ui_statements";   cik = "0001511737" },
  @{ name = "aapl_statements"; cik = "0000320193" },
  @{ name = "msft_statements"; cik = "0000789019" }
)

foreach ($t in $targets) {
  python edgar_extract.py --cik $t.cik --out ("statements\" + $t.name) --years 6 --limit 6
}
```

This produces multiple export folders under `statements/`, which the viewer will pick up automatically.

---

## Run the viewer (edgar_viewer.py)

### 1) Point the viewer at your super-root

`EDGAR_OUT_ROOT` should point to the directory containing one or more export collections.

Example:

```
C:\Users\you\Documents\SEC Edgar\statements
├── ui_statements
├── aapl_statements
└── msft_statements
```

**Windows (PowerShell)**
```powershell
$env:EDGAR_OUT_ROOT = "C:\Users\you\Documents\SEC Edgar\statements"
uvicorn edgar_viewer:app --reload --port 8000
```

**macOS/Linux**
```bash
export EDGAR_OUT_ROOT="/path/to/statements"
uvicorn edgar_viewer:app --reload --port 8000
```

Open:
- http://127.0.0.1:8000/

---

## Output format

### `manifest.json` (per CIK)
Contains:
- filings (accession, form, filingDate, reportDate)
- chosen reports for BS/IS/CFS (short/long names + EDGAR URLs)
- output paths for CSV/JSON
- any parse errors per filing

### `*_statement.json`
Each statement JSON contains:
- `rows`: table rows (first row is header)
- `indent`: integer indentation per row for hierarchy
- `indent_mode`: `"from_html"` or `"inferred"`
- `sourceUrl`: EDGAR report URL used
- `report`: metadata describing the selected report

The viewer uses `indent` to build the collapsible tree.

---

## Troubleshooting

### PowerShell: `export` not recognized
PowerShell uses:
```powershell
$env:VAR = "value"
```
not `export VAR=value`.

### Viewer says “No manifests found”
Check:
1) `EDGAR_OUT_ROOT` is correct  
2) Your exports include a `manifest.json` at either:
- `ROOT/<CIK>/manifest.json`
- `ROOT/<collection>/<CIK>/manifest.json`

### Some filings have missing statements
A filing may use unusual naming or different report structure. Look at `errors` inside the relevant filing entry in the company’s `manifest.json`.

---

## Development notes

- The extractor includes a small rate limiter and retry/backoff for transient errors.
- File writes use safe path resolution to avoid writing outside the chosen output directory.
- The viewer is intentionally server-side only: it serves HTML pages and reads JSON locally.

---

## Suggested `.gitignore`

If you don’t want to commit generated statement datasets:

```gitignore
__pycache__/
*.pyc
.venv/
venv/
.env

statements/
sec_statements_out/
```

---

## License

Add a `LICENSE` file if you want a formal open-source license (MIT is common for portfolio projects).
