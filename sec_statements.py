#!/usr/bin/env python3
import argparse
import csv
import datetime as dt
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, FeatureNotFound

SEC_SUBMISSIONS = "https://data.sec.gov/submissions"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

DEFAULT_TIMEOUT = 40
DEFAULT_MIN_INTERVAL = 0.25
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

# Accept: 34940, 34,940, $34,940, $ 34,940, (4,774), ($ 4,774), -123
NUMISH_RE = re.compile(r"^\s*\(?\s*-?\s*\$?\s*\d[\d,]*([.]\d+)?\s*\)?\s*$")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
HEADER_WORD_RE = re.compile(r"\b(months|years)\s+ended\b|\bas\s+of\b|\bended\b", re.IGNORECASE)

# CSS indentation
CSS_RULE_RE = re.compile(
    r"\.([A-Za-z0-9_-]+)\s*\{[^}]*?(padding-left|margin-left|text-indent)\s*:\s*([0-9.]+)\s*(px|pt|em|rem)\s*;?[^}]*\}",
    re.IGNORECASE | re.DOTALL,
)
STYLE_INDENT_RE = re.compile(
    r"(padding-left|margin-left|text-indent)\s*:\s*([0-9.]+)\s*(px|pt|em|rem)",
    re.IGNORECASE,
)

# Class-name indentation heuristics (covers many EDGAR variants)
CLASS_LEVEL_RE = re.compile(r"^(?:pl|padl|indent|lvl|level)[-_]?(\d+)$", re.IGNORECASE)
CLASS_LEVEL_RE2 = re.compile(r"^(?:pl|lvl|level)(\d+)$", re.IGNORECASE)

# XBRL scaffolding rows
SCAFFOLD_RE = re.compile(r"\[(?:abstract|line items|table|axis|member)\]\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Filing:
    form: str
    filing_date: dt.date
    report_date: Optional[dt.date]
    accession: str


@dataclass(frozen=True)
class Report:
    short_name: str
    long_name: str
    html_file: str
    report_type: str


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = float(min_interval)
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        dt_s = now - self._last
        if dt_s < self.min_interval:
            time.sleep(self.min_interval - dt_s)
        self._last = time.monotonic()


class SecClient:
    def __init__(self, user_agent: str, timeout: int, min_interval: float, max_bytes: int):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/html, application/xml;q=0.9, */*;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self.timeout = int(timeout)
        self.rl = RateLimiter(min_interval)
        self.max_bytes = int(max_bytes)

    def _get(self, url: str) -> requests.Response:
        backoff = 1.0
        last_exc: Optional[Exception] = None
        for _ in range(7):
            try:
                self.rl.wait()
                r = self.session.get(url, timeout=self.timeout)
                if r.status_code == 200:
                    if len(r.content) > self.max_bytes:
                        raise RuntimeError(f"Response too large ({len(r.content)} bytes): {url}")
                    return r
                if r.status_code == 404:
                    return r
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(backoff)
                    backoff = min(backoff * 2.0, 16.0)
                    continue
                r.raise_for_status()
                return r
            except Exception as e:
                last_exc = e
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 16.0)
        raise RuntimeError(f"Failed to fetch {url}: {last_exc}")

    def get_json(self, url: str) -> dict:
        r = self._get(url)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} for {url}")
        return r.json()

    def get_text(self, url: str) -> Tuple[int, str]:
        r = self._get(url)
        return r.status_code, r.text

    def get_bytes(self, url: str) -> Tuple[int, bytes]:
        r = self._get(url)
        return r.status_code, r.content


def norm_cik(cik: str) -> str:
    s = re.sub(r"\D", "", (cik or "").strip())
    if not s:
        raise SystemExit("CIK must be numeric.")
    return s.zfill(10)


def cik_int(cik10: str) -> str:
    return str(int(cik10))


def acc_nodash(accn: str) -> str:
    return accn.replace("-", "")


def parse_ymd(s: Optional[str]) -> Optional[dt.date]:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(str(s))
    except Exception:
        return None


def safe_join(root: Path, rel: Path) -> Path:
    out = (root / rel).resolve()
    rr = root.resolve()
    if out != rr and rr not in out.parents:
        raise RuntimeError("Refusing to write outside output directory.")
    return out


def write_text(root: Path, rel: Path, text: str) -> Path:
    p = safe_join(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def make_soup(html: str) -> BeautifulSoup:
    for parser in ("lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except FeatureNotFound:
            continue
    return BeautifulSoup(html, "html.parser")


def to_px(val: float, unit: str) -> float:
    u = unit.lower()
    if u == "px":
        return val
    if u == "pt":
        return val * (96.0 / 72.0)
    if u in ("em", "rem"):
        return val * 16.0
    return val


def build_css_indent_map(soup: BeautifulSoup) -> Dict[str, float]:
    m: Dict[str, float] = {}
    for st in soup.find_all("style"):
        css = st.get_text(" ", strip=False) or ""
        for cls, _, num, unit in CSS_RULE_RE.findall(css):
            px = to_px(float(num), unit)
            prev = m.get(cls)
            if prev is None or px > prev:
                m[cls] = px
    return m


def is_numericish(s: str) -> bool:
    s = (s or "").replace("\u00a0", " ").strip()
    if not s:
        return False
    if s in ("—", "-", "–"):
        return True
    return bool(NUMISH_RE.match(s))


def row_has_header_hint(row: List[str]) -> bool:
    blob = " ".join((c or "") for c in row).replace("\u00a0", " ").strip()
    if not blob:
        return False
    return bool(YEAR_RE.search(blob) or HEADER_WORD_RE.search(blob))


def gather_filings(client: SecClient, cik10: str) -> List[dict]:
    base = client.get_json(f"{SEC_SUBMISSIONS}/CIK{cik10}.json")
    rows: List[dict] = []

    def add(subj: dict) -> None:
        recent = (subj.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        fdates = recent.get("filingDate") or []
        rdates = recent.get("reportDate") or []
        accs = recent.get("accessionNumber") or []

        n = min(len(forms), len(fdates), len(rdates), len(accs))
        for i in range(n):
            rows.append(
                {
                    "form": str(forms[i]),
                    "filingDate": str(fdates[i]),
                    "reportDate": str(rdates[i]) if rdates[i] else None,
                    "accessionNumber": str(accs[i]),
                }
            )

    add(base)
    for page in (base.get("filings") or {}).get("files") or []:
        name = page.get("name")
        if not name:
            continue
        extra = client.get_json(f"{SEC_SUBMISSIONS}/{name}")
        add(extra)

    return rows


def pick_10ks(rows: List[dict], years_lookback: int, limit: int, include_amends: bool) -> List[Filing]:
    cutoff = dt.date.today() - dt.timedelta(days=int(years_lookback * 365.25))
    ok_forms = {"10-K"} | ({"10-K/A"} if include_amends else set())

    filings: List[Filing] = []
    for r in rows:
        form = (r.get("form") or "").strip()
        if form not in ok_forms:
            continue
        fd = parse_ymd(r.get("filingDate"))
        if not fd or fd < cutoff:
            continue
        acc = (r.get("accessionNumber") or "").strip()
        if not acc:
            continue
        filings.append(Filing(form=form, filing_date=fd, report_date=parse_ymd(r.get("reportDate")), accession=acc))

    filings.sort(key=lambda f: f.filing_date, reverse=True)

    seen = set()
    out: List[Filing] = []
    for f in filings:
        if f.accession in seen:
            continue
        seen.add(f.accession)
        out.append(f)
        if len(out) >= limit:
            break
    return out


def fetch_filing_summary(client: SecClient, base_dir: str) -> Tuple[str, str]:
    for name in ("FilingSummary.xml", "filingsummary.xml"):
        url = f"{base_dir}/{name}"
        code, txt = client.get_text(url)
        if code == 200 and "<FilingSummary" in txt:
            return txt, url

    code, b = client.get_bytes(f"{base_dir}/index.json")
    if code != 200:
        raise RuntimeError("FilingSummary.xml not found (direct) and index.json unavailable.")
    idx = json.loads(b.decode("utf-8", errors="replace"))
    items = (((idx.get("directory") or {}).get("item")) or [])
    cand = None
    for it in items:
        nm = (it.get("name") or "")
        if nm.lower() == "filingsummary.xml":
            cand = nm
            break
    if not cand:
        raise RuntimeError("FilingSummary.xml not present in index.json listing.")
    url = f"{base_dir}/{cand}"
    code, txt = client.get_text(url)
    if code != 200:
        raise RuntimeError(f"HTTP {code} for {url}")
    return txt, url


def parse_reports(filing_summary_xml: str) -> List[Report]:
    root = ET.fromstring(filing_summary_xml)
    reps: List[Report] = []
    for rep in root.findall(".//Report"):
        short = (rep.findtext("ShortName") or "").strip()
        longn = (rep.findtext("LongName") or "").strip()
        htmlf = (rep.findtext("HtmlFileName") or "").strip()
        rtype = (rep.findtext("ReportType") or "").strip()
        if htmlf:
            reps.append(Report(short_name=short, long_name=longn, html_file=Path(htmlf).name, report_type=rtype))
    return reps


def pick_report(reports: List[Report], kind: str) -> Optional[Report]:
    kind = kind.upper()

    if kind == "BS":
        must = ["balance sheet", "financial position", "statement of financial position"]
        avoid = ["parenthetical", "changes in", "equity", "cash flows", "operations", "income", "earnings"]
    elif kind == "IS":
        must = [
            "statement of operations",
            "statements of operations",
            "income statement",
            "statements of income",
            "statement of earnings",
            "statements of earnings",
            "results of operations",
        ]
        avoid = ["comprehensive", "parenthetical", "balance sheet", "cash flows", "equity"]
    elif kind == "CFS":
        must = ["cash flows", "cash flow"]
        avoid = ["parenthetical", "balance sheet", "operations", "income", "earnings", "equity"]
    else:
        return None

    def score(r: Report) -> int:
        t = f"{r.short_name} {r.long_name}".lower()
        s = 0
        for m in must:
            if m in t:
                s += 10
        for a in avoid:
            if a in t:
                s -= 8
        if r.html_file.lower().endswith((".htm", ".html")):
            s += 1
        if r.report_type.lower() in ("sheet", "statement"):
            s += 1
        return s

    best = None
    best_s = -10**9
    for r in reports:
        sc = score(r)
        if sc > best_s:
            best_s = sc
            best = r
    return best if best and best_s > 0 else None


def extract_indent_px(label_cell, css_map: Dict[str, float]) -> int:
    best = 0.0

    # Inline style on the cell + descendants
    def apply_style(style: str):
        nonlocal best
        for _, num, unit in STYLE_INDENT_RE.findall(style or ""):
            best = max(best, to_px(float(num), unit))

    apply_style(label_cell.get("style") or "")
    for node in label_cell.find_all(True, attrs={"style": True}):
        apply_style(node.get("style") or "")

    # CSS class rules from <style>
    classes = label_cell.get("class") or []
    for cls in classes:
        if cls in css_map:
            best = max(best, css_map[cls])

    # Heuristic: class encodes a level (pl1, indent2, lvl3, etc.)
    for cls in classes:
        m = CLASS_LEVEL_RE.match(cls) or CLASS_LEVEL_RE2.match(cls)
        if m:
            lvl = int(m.group(1))
            best = max(best, float(lvl) * 12.0)

    # Leading NBSP as actual unicode (most common in BS4 parsing)
    raw_text = label_cell.get_text("", strip=False) or ""
    i = 0
    nb = 0
    while i < len(raw_text) and raw_text[i] in ("\u00a0", " "):
        if raw_text[i] == "\u00a0":
            nb += 1
        i += 1
    if nb:
        best = max(best, nb * 4.0)

    return int(round(best))


def extract_concepts(label_cell) -> List[str]:
    # EDGAR iXBRL often uses ix:nonnumeric / ix:nonfraction etc
    concepts = []
    for ix in label_cell.find_all(re.compile(r"^(ix:)?(nonfraction|nonnumeric)$", re.IGNORECASE)):
        nm = ix.get("name")
        if nm:
            concepts.append(str(nm))
    # de-dupe while preserving order
    out = []
    seen = set()
    for c in concepts:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out


def extract_table_rows(table, css_map: Dict[str, float]) -> Tuple[List[List[str]], List[int], List[dict]]:
    rows_out: List[List[str]] = []
    indent_px_out: List[int] = []
    meta_out: List[dict] = []

    # col_index -> (remaining_rows, text)
    span_map: Dict[int, Tuple[int, str]] = {}

    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells and not span_map:
            continue

        row: List[str] = []
        col = 0

        def fill_from_span():
            nonlocal col
            while col in span_map:
                remaining, txt = span_map[col]
                row.append(txt)
                if remaining <= 1:
                    del span_map[col]
                else:
                    span_map[col] = (remaining - 1, txt)
                col += 1

        fill_from_span()

        indent_px = extract_indent_px(cells[0], css_map) if cells else 0
        concepts = extract_concepts(cells[0]) if cells else []

        for cell in cells:
            fill_from_span()
            txt = cell.get_text(" ", strip=True).replace("\u00a0", " ")

            try:
                colspan = int(cell.get("colspan") or "1")
            except Exception:
                colspan = 1
            try:
                rowspan = int(cell.get("rowspan") or "1")
            except Exception:
                rowspan = 1

            for _ in range(max(1, colspan)):
                row.append(txt)
                if rowspan > 1:
                    span_map[col] = (rowspan - 1, txt)
                col += 1

        fill_from_span()

        if any((x or "").strip() for x in row):
            rows_out.append(row)
            indent_px_out.append(indent_px)
            meta_out.append({"concepts": concepts})

    if not rows_out:
        return [], [], []

    width = max(len(r) for r in rows_out)
    for r in rows_out:
        if len(r) < width:
            r.extend([""] * (width - len(r)))
    return rows_out, indent_px_out, meta_out


def table_profile(rows: List[List[str]]) -> Tuple[int, int, int, int]:
    if not rows:
        return 0, 0, 0, 0
    col_count = max(len(r) for r in rows)
    numeric_cells = 0
    year_cells = 0
    nonempty = 0
    for r in rows:
        for c in r:
            t = (c or "").replace("\u00a0", " ").strip()
            if not t:
                continue
            nonempty += 1
            if is_numericish(t):
                numeric_cells += 1
            if YEAR_RE.search(t):
                year_cells += 1
    return col_count, numeric_cells, year_cells, nonempty


def merge_multiline_headers(rows: List[List[str]], indent_px: List[int], meta: List[dict]) -> Tuple[List[List[str]], List[int], List[dict]]:
    if not rows:
        return [], [], []

    width = max(len(r) for r in rows)
    for r in rows:
        if len(r) < width:
            r.extend([""] * (width - len(r)))

    header_block = []
    for r in rows[:10]:
        vals = [v for v in r[1:] if (v or "").strip()]
        if not vals:
            break
        if any(is_numericish(v) for v in vals):
            break
        if not row_has_header_hint(r):
            break
        header_block.append(r)

    if len(header_block) < 2:
        return rows, indent_px, meta

    col_count = width - 1
    cols = [""] * col_count
    for hr in header_block:
        for j in range(col_count):
            part = (hr[j + 1] or "").strip()
            if part:
                cols[j] = (cols[j] + " " + part).strip()

    merged = [header_block[0][0]] + cols
    new_rows = [merged] + rows[len(header_block):]
    new_indent = [indent_px[0] if indent_px else 0] + indent_px[len(header_block):]
    new_meta = [meta[0] if meta else {}] + meta[len(header_block):]
    return new_rows, new_indent, new_meta


def select_and_stitch_tables(soup: BeautifulSoup) -> Tuple[List[List[str]], List[int], List[dict]]:
    css_map = build_css_indent_map(soup)
    tables = soup.find_all("table")
    if not tables:
        return [], [], []

    candidates: List[Tuple[int, int, List[List[str]], List[int], List[dict], int, int]] = []
    # (score, idx, rows, indent_px, meta, col_count, numeric_cells)

    for idx, tbl in enumerate(tables):
        rows, ind_px, meta = extract_table_rows(tbl, css_map)
        if not rows:
            continue
        colc, numc, yearc, nonempty = table_profile(rows)
        score = (numc * 3) + (yearc * 2) + min(len(rows), 220)
        if colc < 2 or nonempty < 12:
            score -= 500
        candidates.append((score, idx, rows, ind_px, meta, colc, numc))

    if not candidates:
        return [], [], []

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_idx, best_rows, best_ind_px, best_meta, base_cols, base_num = candidates[0]

    def looks_like_continuation(rows: List[List[str]], base_cols: int, base_num: int) -> bool:
        colc, numc, _, nonempty = table_profile(rows)
        if colc != base_cols:
            return False
        if nonempty < 8:
            return False
        return numc >= max(6, int(base_num * 0.12))

    by_doc = sorted([(idx, rows, ind_px, meta) for (_, idx, rows, ind_px, meta, _, _) in candidates], key=lambda x: x[0])
    start_pos = next(i for i, (idx, _, _, _) in enumerate(by_doc) if idx == best_idx)

    combined_rows = list(best_rows)
    combined_ind_px = list(best_ind_px)
    combined_meta = list(best_meta)

    def norm_row(r: List[str]) -> str:
        return " | ".join((c or "").replace("\u00a0", " ").strip().lower() for c in r)

    head_sig = norm_row(combined_rows[0]) if combined_rows else ""

    for k in range(start_pos + 1, min(start_pos + 4, len(by_doc))):
        _, rows2, ind2, meta2 = by_doc[k]
        if not looks_like_continuation(rows2, base_cols, base_num):
            break

        drop = 0
        for t in range(min(3, len(rows2))):
            if norm_row(rows2[t]) == head_sig:
                drop = t + 1
        combined_rows.extend(rows2[drop:])
        combined_ind_px.extend(ind2[drop:])
        combined_meta.extend(meta2[drop:])

    return merge_multiline_headers(combined_rows, combined_ind_px, combined_meta)


def values_blank(row: List[str]) -> bool:
    return all(not (c or "").strip() for c in row[1:])


def filter_scaffolding(rows: List[List[str]], indent_px: List[int], meta: List[dict], keep_abstract: bool) -> Tuple[List[List[str]], List[int], List[dict]]:
    out_rows, out_ind, out_meta = [], [], []
    for r, ipx, m in zip(rows, indent_px, meta):
        label = (r[0] or "").strip()
        if not label:
            continue

        concepts = m.get("concepts") or []
        is_abstract_by_label = bool(SCAFFOLD_RE.search(label))
        is_abstract_by_concept = any(str(c).lower().endswith("abstract") for c in concepts)

        # Drop pure scaffolding rows (typically empty value columns)
        if not keep_abstract:
            if (is_abstract_by_label or is_abstract_by_concept) and values_blank(r):
                continue

        out_rows.append(r)
        out_ind.append(ipx)
        out_meta.append({**m, "scaffold": (is_abstract_by_label or is_abstract_by_concept)})

    return out_rows, out_ind, out_meta


def infer_indent_levels(rows: List[List[str]], stmt: str) -> List[int]:
    # Only used when HTML indentation is missing (all zeros).
    # Goal: produce a stable hierarchy that matches how statements are read.
    stmt = stmt.upper()
    levels = [0] * len(rows)

    major_cfs = re.compile(r"^(operating|investing|financing)\s+activities:\s*$", re.IGNORECASE)
    sub_adjust = re.compile(r"^adjustments\b", re.IGNORECASE)
    sub_changes = re.compile(r"^changes in\b", re.IGNORECASE)

    in_major = False
    in_adjust = False
    in_changes = False

    for i, r in enumerate(rows):
        if i == 0:
            levels[i] = 0
            continue

        label = (r[0] or "").strip()
        blank_vals = values_blank(r)

        if not blank_vals:
            if stmt == "CFS":
                if in_changes:
                    levels[i] = 3
                elif in_adjust:
                    levels[i] = 2
                elif in_major:
                    levels[i] = 1
                else:
                    levels[i] = 1
            else:
                levels[i] = 1
            continue

        # header rows (blank values)
        if stmt == "CFS":
            if major_cfs.match(label):
                in_major = True
                in_adjust = False
                in_changes = False
                levels[i] = 0
            elif sub_adjust.match(label):
                in_adjust = True
                in_changes = False
                levels[i] = 1
            elif sub_changes.match(label):
                # usually nested under Adjustments in cash flows
                in_changes = True
                levels[i] = 2 if in_adjust else 1
            else:
                # generic headers: keep them at the current context level
                if in_changes:
                    levels[i] = 2
                elif in_adjust:
                    levels[i] = 1
                else:
                    levels[i] = 0
        else:
            # BS / IS: headers (like "Current assets:", "Operating expenses:") at level 0
            levels[i] = 0

    return levels


def write_csv_file(path: Path, rows: List[List[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="CIK -> last 10-Ks -> rebuild BS/IS/CFS as presented (EDGAR R*.htm).")
    ap.add_argument("--cik", required=True, help="Company CIK (digits).")
    ap.add_argument("--years", type=int, default=5, help="Lookback window (years).")
    ap.add_argument("--limit", type=int, default=5, help="Max number of 10-K filings to process.")
    ap.add_argument("--out", default="sec_statements_out", help="Output directory.")
    ap.add_argument("--user-agent", default=os.getenv("SEC_UA", ""), help="User-Agent with contact info.")
    ap.add_argument("--include-amends", action="store_true", help="Include 10-K/A.")
    ap.add_argument("--keep-abstract", action="store_true", help="Keep XBRL scaffolding rows like [Abstract].")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL)
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = ap.parse_args()

    ua = (args.user_agent or "").strip()
    if not ua:
        raise SystemExit('Provide --user-agent "app (email@domain)" or set SEC_UA env var.')

    cik10 = norm_cik(args.cik)
    client = SecClient(ua, timeout=args.timeout, min_interval=args.min_interval, max_bytes=args.max_bytes)

    rows = gather_filings(client, cik10)
    filings = pick_10ks(rows, years_lookback=args.years, limit=args.limit, include_amends=args.include_amends)
    if not filings:
        raise SystemExit("No matching 10-K filings found in the requested window.")

    out_root = Path(args.out).resolve() / cik10
    out_root.mkdir(parents=True, exist_ok=True)

    manifest = {
        "cik": cik10,
        "generatedAt": dt.datetime.utcnow().isoformat() + "Z",
        "filings": [],
    }

    cik_i = cik_int(cik10)

    for f in filings:
        acc = f.accession
        base_dir = f"{SEC_ARCHIVES}/{cik_i}/{acc_nodash(acc)}"
        filing_dir = out_root / acc
        filing_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "accessionNumber": acc,
            "form": f.form,
            "filingDate": f.filing_date.isoformat(),
            "reportDate": f.report_date.isoformat() if f.report_date else None,
            "baseUrl": base_dir,
            "reportsPicked": {},
            "outputs": {},
            "errors": [],
        }

        try:
            fs_xml, fs_url = fetch_filing_summary(client, base_dir)
            write_text(filing_dir, Path("FilingSummary.xml"), fs_xml)
            entry["filingSummaryUrl"] = fs_url
        except Exception as e:
            entry["errors"].append(f"FilingSummary: {e}")
            manifest["filings"].append(entry)
            continue

        reports = parse_reports(fs_xml)
        picked = {
            "BS": pick_report(reports, "BS"),
            "IS": pick_report(reports, "IS"),
            "CFS": pick_report(reports, "CFS"),
        }

        for key, rep in picked.items():
            if not rep:
                entry["errors"].append(f"{key}: report not found in FilingSummary.xml")
                continue

            rep_url = f"{base_dir}/{rep.html_file}"
            code, html = client.get_text(rep_url)
            if code != 200:
                entry["errors"].append(f"{key}: HTTP {code} for {rep.html_file}")
                continue

            write_text(filing_dir, Path(rep.html_file), html)

            soup = make_soup(html)
            stmt_rows, indent_px, meta = select_and_stitch_tables(soup)
            if not stmt_rows:
                entry["errors"].append(f"{key}: could not parse statement tables from {rep.html_file}")
                continue

            # Drop XBRL scaffolding rows (Abstract / Line Items / etc.)
            stmt_rows, indent_px, meta = filter_scaffolding(stmt_rows, indent_px, meta, keep_abstract=args.keep_abstract)

            # If indentation is missing, infer stable indent levels
            if all((v or 0) == 0 for v in indent_px):
                indent = infer_indent_levels(stmt_rows, key)
                indent_mode = "inferred"
            else:
                # convert px -> level for stability
                indent = [int(round((v or 0) / 12.0)) for v in indent_px]
                indent_mode = "from_html"

            stem = {"BS": "balance_sheet", "IS": "income_statement", "CFS": "cash_flow"}[key]
            csv_path = filing_dir / f"{stem}.csv"
            json_path = filing_dir / f"{stem}.json"

            write_csv_file(csv_path, stmt_rows)

            json_payload = {
                "cik": cik10,
                "accessionNumber": acc,
                "statement": key,
                "sourceUrl": rep_url,
                "report": {
                    "short": rep.short_name,
                    "long": rep.long_name,
                    "html": rep.html_file,
                    "type": rep.report_type,
                },
                "indent_mode": indent_mode,
                "indent": indent,
                "rows": stmt_rows,
                "row_meta": meta,
            }
            json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")

            entry["reportsPicked"][key] = {
                "short": rep.short_name,
                "long": rep.long_name,
                "file": rep.html_file,
                "type": rep.report_type,
                "url": rep_url,
            }
            entry["outputs"][key] = {"csv": str(csv_path), "json": str(json_path)}

        manifest["filings"].append(entry)

    write_text(out_root, Path("manifest.json"), json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({"out": str(out_root), "processed": len(manifest["filings"])}, indent=2))


if __name__ == "__main__":
    main()
