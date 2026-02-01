#!/usr/bin/env python3
import datetime as dt
import html
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

app = FastAPI()

# Point this to a "super-root" that contains multiple exports, e.g.:
#   C:\Users\second\Documents\SEC Edgar\statements
ROOT = Path(os.getenv("EDGAR_OUT_ROOT", "statements")).resolve()

# Internal label for manifests located directly under ROOT\<CIK>\manifest.json
ROOT_COLLECTION = "__root__"


def _safe_resolve(root: Path, rel: Path) -> Path:
    out = (root / rel).resolve()
    rr = root.resolve()
    if out != rr and rr not in out.parents:
        raise HTTPException(status_code=400, detail="Invalid path.")
    return out


def _load_json(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found.")
    except Exception:
        raise HTTPException(status_code=500, detail="Bad JSON.")


def _parse_iso_date(s: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(s))
    except Exception:
        return None


def _year_from_report_date(f: dict) -> str:
    rd = _parse_iso_date(f.get("reportDate") or "")
    return str(rd.year) if rd else "Unknown year"


def _values_blank(row: List[str]) -> bool:
    return all(not (c or "").strip() for c in row[1:])


def _compute_paths(levels: List[int]) -> List[str]:
    # Stable dotted paths (e.g. "1", "1.1", "1.2", "2", "2.1.1")
    stack: List[int] = [0]
    paths: List[str] = []
    for lvl in levels:
        if lvl < 0:
            lvl = 0
        while (len(stack) - 1) > lvl:
            stack.pop()
        while (len(stack) - 1) < lvl:
            stack.append(0)
        stack[-1] += 1
        paths.append(".".join(str(x) for x in stack))
    return paths


def _collection_base(collection: str) -> Path:
    if collection == ROOT_COLLECTION:
        return ROOT
    return _safe_resolve(ROOT, Path(collection))


def _discover_manifests(root: Path) -> List[Tuple[str, str, Path]]:
    """
    Returns (collection_name, cik, manifest_path).

    Supported layouts:
      - ROOT/<CIK>/manifest.json                      -> collection="__root__"
      - ROOT/<collection>/<CIK>/manifest.json        -> collection=<collection>
    """
    out: List[Tuple[str, str, Path]] = []
    if not root.exists():
        return out

    # Layout A: ROOT/<CIK>/manifest.json
    for d in root.iterdir():
        if not d.is_dir():
            continue
        mf = d / "manifest.json"
        if mf.exists():
            out.append((ROOT_COLLECTION, d.name, mf))

    # Layout B: ROOT/<collection>/<CIK>/manifest.json
    for collection_dir in root.iterdir():
        if not collection_dir.is_dir():
            continue
        # If this folder itself is a CIK folder (already counted), skip
        if (collection_dir / "manifest.json").exists():
            continue

        for cik_dir in collection_dir.iterdir():
            if not cik_dir.is_dir():
                continue
            mf = cik_dir / "manifest.json"
            if mf.exists():
                out.append((collection_dir.name, cik_dir.name, mf))

    out.sort(key=lambda x: (x[0], x[1], str(x[2])))
    return out


def _pick_collection_for_cik(cik: str) -> Optional[str]:
    # If CIK exists in multiple collections, pick deterministically:
    # prefer __root__ first, otherwise alphabetical by collection name.
    entries = _discover_manifests(ROOT)  # (collection, cik, path)
    hits = [c for (c, k, _) in entries if k == cik]
    if not hits:
        return None
    if ROOT_COLLECTION in hits:
        return ROOT_COLLECTION
    return sorted(hits)[0]


def _render_statement(payload: Dict[str, Any]) -> str:
    rows: List[List[str]] = payload.get("rows") or []
    indent: List[int] = payload.get("indent") or []
    source_url = str(payload.get("sourceUrl") or "")
    report = payload.get("report") or {}
    title = (rows[0][0] if rows and rows[0] else report.get("short") or "Statement") or "Statement"

    if not rows:
        raise HTTPException(status_code=500, detail="Empty statement.")

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    if len(indent) != len(rows):
        indent = [0] * len(rows)

    hdr = rows[0]
    col_headers = hdr[1:]

    body_rows = rows[1:]
    body_levels = indent[1:]
    paths = _compute_paths(body_levels)

    has_child = [False] * len(body_rows)
    for i in range(len(body_rows) - 1):
        if body_levels[i + 1] > body_levels[i]:
            has_child[i] = True

    esc_title = html.escape(title)
    esc_source = html.escape(source_url)

    tr_html: List[str] = []
    for i, (r, lvl, path) in enumerate(zip(body_rows, body_levels, paths)):
        label = (r[0] or "").strip()
        vals = r[1:]
        is_section = _values_blank(r)
        is_total = ("total" in label.lower()) and any((v or "").strip() for v in vals)
        pad = max(0, int(lvl)) * 18

        if has_child[i]:
            caret = f'<button class="twisty" data-toggle="{html.escape(path)}" aria-label="Toggle">▾</button>'
        else:
            caret = '<span class="twisty-spacer"></span>'

        cls = "row"
        if is_section:
            cls += " section"
        if is_total:
            cls += " total"

        tds: List[str] = []
        tds.append(
            f"""
            <td class="label" style="padding-left:{pad}px">
              {caret}
              <span class="label-text">{html.escape(label)}</span>
            </td>
            """
        )
        for v in vals:
            tds.append(f'<td class="num">{html.escape((v or "").strip())}</td>')

        tr_html.append(f'<tr class="{cls}" data-path="{html.escape(path)}">{"".join(tds)}</tr>')

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{esc_title}</title>
<style>
  :root {{
    --bg: #0b0d10;
    --panel: #0f1318;
    --text: #e9eef5;
    --muted: #a6b2c2;
    --line: #1c2530;
    --accent: #6ea8ff;
    --section: #0d1622;
  }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
  }}
  .wrap {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 24px 16px 40px;
  }}
  .top {{
    display: flex;
    gap: 12px;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }}
  h1 {{
    font-size: 18px;
    margin: 0;
    color: var(--text);
    letter-spacing: 0.2px;
  }}
  .meta {{
    display:flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
    color: var(--muted);
    font-size: 13px;
  }}
  .meta a {{
    color: var(--accent);
    text-decoration: none;
  }}
  .controls {{
    display:flex;
    gap: 8px;
    flex-wrap: wrap;
    margin: 10px 0 14px;
  }}
  .btn {{
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--line);
    padding: 8px 10px;
    border-radius: 10px;
    cursor: pointer;
    font-size: 13px;
  }}
  .btn:hover {{ border-color: #2a3a4a; }}
  .search {{
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--line);
    padding: 8px 10px;
    border-radius: 10px;
    font-size: 13px;
    min-width: 240px;
    outline: none;
  }}
  .card {{
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 16px;
    overflow: hidden;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
  }}
  thead th {{
    position: sticky;
    top: 0;
    background: #0c1117;
    z-index: 2;
    text-align: right;
    padding: 10px 12px;
    border-bottom: 1px solid var(--line);
    color: var(--muted);
    font-weight: 600;
    font-size: 12px;
    white-space: nowrap;
  }}
  thead th:first-child {{
    text-align: left;
  }}
  tbody td {{
    padding: 10px 12px;
    border-bottom: 1px solid var(--line);
    font-size: 13px;
    vertical-align: top;
  }}
  tbody td.num {{
    text-align: right;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }}
  tbody tr:hover td {{
    background: rgba(255,255,255,0.02);
  }}
  td.label {{
    text-align: left;
    white-space: normal;
  }}
  .section td {{
    background: var(--section);
    font-weight: 700;
    color: var(--text);
  }}
  .total td {{
    font-weight: 700;
  }}
  .twisty {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    margin-right: 6px;
    border-radius: 8px;
    border: 1px solid var(--line);
    background: transparent;
    color: var(--muted);
    cursor: pointer;
  }}
  .twisty:hover {{
    border-color: #2a3a4a;
    color: var(--text);
  }}
  .twisty-spacer {{
    display:inline-block;
    width: 20px;
    height: 20px;
    margin-right: 6px;
  }}
  .hidden {{
    display: none;
  }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>{esc_title}</h1>
      <div class="meta">
        <span>Indent: {html.escape(str(payload.get("indent_mode") or "unknown"))}</span>
        <span>•</span>
        <a href="{esc_source}" target="_blank" rel="noreferrer">Open source filing table</a>
      </div>
    </div>

    <div class="controls">
      <button class="btn" id="expandAll">Expand all</button>
      <button class="btn" id="collapseAll">Collapse all</button>
      <input class="search" id="q" placeholder="Filter labels (substring)…" />
      <button class="btn" id="clear">Clear</button>
    </div>

    <div class="card">
      <table>
        <thead>
          <tr>
            <th>Line item</th>
            {"".join(f"<th>{html.escape((h or '').strip())}</th>" for h in col_headers)}
          </tr>
        </thead>
        <tbody id="tbody">
          {"".join(tr_html)}
        </tbody>
      </table>
    </div>
  </div>

<script>
(() => {{
  const tbody = document.getElementById('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const collapsed = new Set();

  const ancestors = (path) => {{
    const parts = path.split('.');
    const out = [];
    for (let i = 1; i < parts.length; i++) {{
      out.push(parts.slice(0, i).join('.'));
    }}
    return out;
  }};

  const underCollapsed = (path) => {{
    for (const a of ancestors(path)) {{
      if (collapsed.has(a)) return true;
    }}
    return false;
  }};

  const setDescendants = (path, visible) => {{
    const prefix = path + '.';
    for (const tr of rows) {{
      const p = tr.dataset.path;
      if (p.startsWith(prefix)) {{
        if (!visible) tr.classList.add('hidden');
        else {{
          if (!underCollapsed(p)) tr.classList.remove('hidden');
        }}
      }}
    }}
  }};

  const updateTwisty = (path) => {{
    const btn = document.querySelector(`button[data-toggle="${path}"]`);
    if (!btn) return;
    btn.textContent = collapsed.has(path) ? '▸' : '▾';
  }};

  const toggle = (path) => {{
    if (collapsed.has(path)) {{
      collapsed.delete(path);
      updateTwisty(path);
      setDescendants(path, true);
    }} else {{
      collapsed.add(path);
      updateTwisty(path);
      setDescendants(path, false);
    }}
  }};

  document.addEventListener('click', (e) => {{
    const btn = e.target.closest('button[data-toggle]');
    if (!btn) return;
    toggle(btn.dataset.toggle);
  }});

  const collapseAll = () => {{
    collapsed.clear();
    const toggles = Array.from(document.querySelectorAll('button[data-toggle]')).map(b => b.dataset.toggle);
    for (const p of toggles) collapsed.add(p);
    for (const p of toggles) updateTwisty(p);
    for (const tr of rows) {{
      const p = tr.dataset.path;
      if (p.includes('.')) tr.classList.add('hidden');
      else tr.classList.remove('hidden');
    }}
  }};

  const expandAll = () => {{
    collapsed.clear();
    document.querySelectorAll('button[data-toggle]').forEach(b => b.textContent = '▾');
    rows.forEach(tr => tr.classList.remove('hidden'));
  }};

  document.getElementById('collapseAll').addEventListener('click', collapseAll);
  document.getElementById('expandAll').addEventListener('click', expandAll);

  const q = document.getElementById('q');
  const clear = document.getElementById('clear');

  const applyFilter = () => {{
    const needle = (q.value || '').trim().toLowerCase();
    if (!needle) {{
      for (const tr of rows) {{
        const p = tr.dataset.path;
        if (underCollapsed(p)) tr.classList.add('hidden');
        else tr.classList.remove('hidden');
      }}
      return;
    }}
    const show = new Set();
    for (const tr of rows) {{
      const label = tr.querySelector('.label-text')?.textContent?.toLowerCase() || '';
      if (label.includes(needle)) {{
        const p = tr.dataset.path;
        show.add(p);
        for (const a of ancestors(p)) show.add(a);
      }}
    }}
    for (const tr of rows) {{
      const p = tr.dataset.path;
      if (show.has(p) && !underCollapsed(p)) tr.classList.remove('hidden');
      else tr.classList.add('hidden');
    }}
  }};

  q.addEventListener('input', applyFilter);
  clear.addEventListener('click', () => {{ q.value = ''; applyFilter(); }});
}})();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    entries = _discover_manifests(ROOT)
    if not entries:
        return f"<h3>No manifests found under {html.escape(str(ROOT))}</h3>"

    cards: List[str] = []
    for collection, cik, mf_path in entries:
        mf = _load_json(mf_path)
        filings = mf.get("filings") or []
        filings_ok = [f for f in filings if (f.get("reportsPicked") or {})]

        def _sort_key(f: dict):
            fd = _parse_iso_date(f.get("filingDate") or "") or dt.date.min
            rd = _parse_iso_date(f.get("reportDate") or "") or dt.date.min
            return (fd, rd)

        filings_ok.sort(key=_sort_key, reverse=True)
        latest = filings_ok[0] if filings_ok else None
        latest_fd = latest.get("filingDate") if latest else ""
        latest_rd = latest.get("reportDate") if latest else ""

        coll_label = "root" if collection == ROOT_COLLECTION else collection

        cards.append(
            f"""
            <div class="card">
              <div class="topline">
                <div class="left">
                  <div class="badge">{html.escape(coll_label)}</div>
                  <div class="cik">CIK {html.escape(cik)}</div>
                </div>
                <a class="open" href="/c/{html.escape(collection)}/{html.escape(cik)}">Open</a>
              </div>
              <div class="meta">
                <span>Filings: {len(filings_ok)}</span>
                <span>•</span>
                <span>Latest filed: {html.escape(latest_fd or "—")}</span>
                <span>•</span>
                <span>Report date: {html.escape(latest_rd or "—")}</span>
              </div>
            </div>
            """
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>EDGAR Statements</title>
<style>
  body {{ margin:0; background:#0b0d10; color:#e9eef5; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; }}
  .wrap {{ max-width: 1100px; margin:0 auto; padding: 24px 16px 44px; }}
  h1 {{ font-size: 18px; margin: 0 0 14px; }}
  .subtitle {{ color:#a6b2c2; font-size:13px; margin: 0 0 16px; }}
  .grid {{ display:grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }}
  .card {{ background:#0f1318; border:1px solid #1c2530; border-radius:16px; padding: 14px; }}
  .topline {{ display:flex; align-items:center; justify-content:space-between; gap:10px; }}
  .left {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .badge {{ color:#a6b2c2; border:1px solid #1c2530; padding:4px 8px; border-radius:999px; font-size:12px; }}
  .cik {{ color:#e9eef5; font-size: 13px; }}
  .open {{ color:#6ea8ff; text-decoration:none; font-size:13px; border:1px solid #1c2530; padding:6px 10px; border-radius:10px; }}
  .open:hover {{ border-color:#2a3a4a; }}
  .meta {{ margin-top:8px; color:#a6b2c2; font-size: 13px; display:flex; gap:10px; flex-wrap:wrap; }}
</style>
</head><body>
  <div class="wrap">
    <h1>EDGAR Statements Viewer</h1>
    <div class="subtitle">Root: {html.escape(str(ROOT))}</div>
    <div class="grid">{''.join(cards)}</div>
  </div>
</body></html>
"""


@app.get("/c/{collection}/{cik}", response_class=HTMLResponse)
def cik_page(collection: str, cik: str, stmt: Optional[str] = None) -> str:
    stmt = (stmt or "").upper().strip()
    if stmt and stmt not in ("BS", "IS", "CFS"):
        raise HTTPException(status_code=400, detail="Bad stmt filter.")

    base = _collection_base(collection)
    mf_path = _safe_resolve(base, Path(cik) / "manifest.json")
    mf = _load_json(mf_path)
    filings = mf.get("filings") or []

    grouped: Dict[str, List[dict]] = {}
    for f in filings:
        year = _year_from_report_date(f)
        grouped.setdefault(year, []).append(f)

    years = sorted([y for y in grouped.keys() if y.isdigit()], key=int, reverse=True)
    if "Unknown year" in grouped:
        years.append("Unknown year")

    def _sort_key_filing(f: dict):
        rd = _parse_iso_date(f.get("reportDate") or "") or dt.date.min
        fd = _parse_iso_date(f.get("filingDate") or "") or dt.date.min
        return (rd, fd)

    sections: List[str] = []
    for y in years:
        fs = grouped[y]
        fs.sort(key=_sort_key_filing, reverse=True)

        rows_html: List[str] = []
        for f in fs:
            acc = str(f.get("accessionNumber") or "")
            form = str(f.get("form") or "")
            filing_date = str(f.get("filingDate") or "")
            report_date = str(f.get("reportDate") or "")
            picked = f.get("reportsPicked") or {}

            btns: List[str] = []
            for k, label in (("BS", "Balance Sheet"), ("IS", "Income Statement"), ("CFS", "Cash Flow")):
                if k not in picked:
                    continue
                if stmt and k != stmt:
                    continue
                btns.append(
                    f'<a class="pill" href="/view/{html.escape(collection)}/{html.escape(cik)}/{html.escape(acc)}/{k}">{label}</a>'
                )

            if not btns:
                continue

            rows_html.append(
                f"""
                <div class="filing">
                  <div class="left">
                    <div class="big">{html.escape(report_date or "—")}</div>
                    <div class="small">Filed {html.escape(filing_date or "—")} • {html.escape(form)} • {html.escape(acc)}</div>
                  </div>
                  <div class="right">
                    {''.join(btns)}
                  </div>
                </div>
                """
            )

        if rows_html:
            sections.append(
                f"""
                <div class="year">
                  <div class="yearhdr">
                    <div class="yearlabel">FY {html.escape(y)}</div>
                  </div>
                  <div class="yearbody">
                    {''.join(rows_html)}
                  </div>
                </div>
                """
            )

    filter_bar = f"""
      <div class="filters">
        <a class="chip {'on' if not stmt else ''}" href="/c/{html.escape(collection)}/{html.escape(cik)}">All</a>
        <a class="chip {'on' if stmt=='BS' else ''}" href="/c/{html.escape(collection)}/{html.escape(cik)}?stmt=BS">BS</a>
        <a class="chip {'on' if stmt=='IS' else ''}" href="/c/{html.escape(collection)}/{html.escape(cik)}?stmt=IS">IS</a>
        <a class="chip {'on' if stmt=='CFS' else ''}" href="/c/{html.escape(collection)}/{html.escape(cik)}?stmt=CFS">CFS</a>
      </div>
    """

    coll_label = "root" if collection == ROOT_COLLECTION else collection

    return f"""<!doctype html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(coll_label)} • {html.escape(cik)}</title>
<style>
  body {{ margin:0; background:#0b0d10; color:#e9eef5; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; }}
  .wrap {{ max-width: 1100px; margin:0 auto; padding: 24px 16px 44px; }}
  .top {{ display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:12px; }}
  h1 {{ font-size:18px; margin:0; }}
  .back {{ color:#6ea8ff; text-decoration:none; font-size:13px; border:1px solid #1c2530; padding:6px 10px; border-radius:10px; }}
  .back:hover {{ border-color:#2a3a4a; }}
  .subtitle {{ color:#a6b2c2; font-size:13px; margin-top:6px; }}

  .filters {{ display:flex; gap:8px; flex-wrap:wrap; margin: 10px 0 16px; }}
  .chip {{ color:#a6b2c2; text-decoration:none; font-size:13px; border:1px solid #1c2530; padding:6px 10px; border-radius:999px; }}
  .chip.on {{ color:#e9eef5; border-color:#2a3a4a; }}

  .year {{ background:#0f1318; border:1px solid #1c2530; border-radius:16px; overflow:hidden; margin-bottom:12px; }}
  .yearhdr {{ padding:12px 14px; background:#0c1117; border-bottom:1px solid #1c2530; display:flex; align-items:center; justify-content:space-between; }}
  .yearlabel {{ font-size:13px; color:#a6b2c2; letter-spacing:0.3px; }}
  .yearbody {{ padding: 6px 10px; }}

  .filing {{ display:flex; gap:12px; align-items:center; justify-content:space-between; flex-wrap:wrap; padding:10px 6px; border-bottom:1px solid #1c2530; }}
  .filing:last-child {{ border-bottom:none; }}
  .left {{ min-width: 260px; }}
  .big {{ font-weight:700; font-size:14px; }}
  .small {{ color:#a6b2c2; font-size:12px; margin-top:2px; }}

  .right {{ display:flex; gap:8px; flex-wrap:wrap; }}
  .pill {{ color:#e9eef5; text-decoration:none; font-size:13px; border:1px solid #1c2530; padding:8px 10px; border-radius:12px; background:#0b0d10; }}
  .pill:hover {{ border-color:#2a3a4a; }}
</style>
</head><body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>{html.escape(coll_label)} • CIK {html.escape(cik)}</h1>
        <div class="subtitle">Grouped by report year (reportDate)</div>
      </div>
      <a class="back" href="/">Back</a>
    </div>
    {filter_bar}
    {''.join(sections) if sections else '<div style="color:#a6b2c2;">No filings found.</div>'}
  </div>
</body></html>
"""


@app.get("/view/{collection}/{cik}/{accession}/{stmt}", response_class=HTMLResponse)
def view_statement(collection: str, cik: str, accession: str, stmt: str) -> str:
    stmt = stmt.upper()
    if stmt not in ("BS", "IS", "CFS"):
        raise HTTPException(status_code=400, detail="Bad statement type.")

    base = _collection_base(collection)
    stem = {"BS": "balance_sheet", "IS": "income_statement", "CFS": "cash_flow"}[stmt]

    rel = Path(cik) / accession / f"{stem}.json"
    p = _safe_resolve(base, rel)
    payload = _load_json(p)
    return _render_statement(payload)


# Backward-compatible routes (old viewer used /cik/<CIK> and /view/<CIK>/<ACC>/<STMT>)

@app.get("/cik/{cik}")
def legacy_cik_redirect(cik: str):
    coll = _pick_collection_for_cik(cik)
    if not coll:
        raise HTTPException(status_code=404, detail="CIK not found under EDGAR_OUT_ROOT.")
    return RedirectResponse(url=f"/c/{coll}/{cik}", status_code=307)


@app.get("/view/{cik}/{accession}/{stmt}")
def legacy_view_redirect(cik: str, accession: str, stmt: str):
    coll = _pick_collection_for_cik(cik)
    if not coll:
        raise HTTPException(status_code=404, detail="CIK not found under EDGAR_OUT_ROOT.")
    stmt = (stmt or "").upper()
    if stmt not in ("BS", "IS", "CFS"):
        raise HTTPException(status_code=400, detail="Bad statement type.")
    return RedirectResponse(url=f"/view/{coll}/{cik}/{accession}/{stmt}", status_code=307)


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)
