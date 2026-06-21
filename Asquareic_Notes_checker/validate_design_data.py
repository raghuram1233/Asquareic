"""
validate_design_data.py
=======================
Cross-check design parameters extracted from a CAD drawing (CSV) against
the approved PV Elite mechanical calculation report (DOCX).

Usage
-----
  python validate_design_data.py
  python validate_design_data.py --docx inputs/other_vessel.docx --csv reports/design_data.csv
  python validate_design_data.py --outdir reports

The validator is tolerant of row misalignment in the extracted CSV — it searches
the entire VALUE column for each expected value rather than relying on row identity.
"""

import argparse
import html
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from docx import Document

# ==============================================================================
# DEFAULTS
# ==============================================================================

DEFAULT_DOCX = "inputs/Biocide Chemical Injection 6209-04.docx"
DEFAULT_CSV  = "reports/design_data.csv"
DEFAULT_OUT  = "reports"

# ==============================================================================
# DATA CLASSES
# ==============================================================================

@dataclass
class Expected:
    name: str            # human-readable field name
    display: str         # formatted expected value string
    needles: list        # list of str/float used for matching
    numeric_tol: float = 0.03   # relative tolerance for numeric matching


@dataclass
class Result:
    name: str
    expected_display: str
    matched_value: str
    status: str          # "PASS" | "FAIL" | "NOT IN DOCX"


# ==============================================================================
# SECTION 1 — LOAD EXTRACTED CAD VALUES
# ==============================================================================

def load_extracted_values(csv_path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Read the extraction CSV. Returns:
      rows  : list of (field, value) tuples
      values: flat list of all VALUE strings (for whole-column search)
    """
    df = pd.read_csv(csv_path)

    # Accept FIELD/VALUE regardless of case; fall back to first two columns
    col_map = {c.upper(): c for c in df.columns}
    f_col = col_map.get("FIELD", df.columns[0])
    v_col = col_map.get("VALUE", df.columns[1])

    rows = [
        (html.unescape(str(r[f_col])).strip(), html.unescape(str(r[v_col])).strip())
        for _, r in df.iterrows()
    ]
    values = [v for _, v in rows]
    return rows, values


# ==============================================================================
# SECTION 2 — PARSE GROUND TRUTH FROM DOCX
# ==============================================================================

_MANGLE_DEG = re.compile(r"[^\x00-\x7F°]C")  # mangled degree symbol → C


def _clean(text: str) -> str:
    """Normalize a paragraph string from the docx."""
    t = _MANGLE_DEG.sub("C", text)
    t = t.replace("\t", " ")
    t = re.sub(r" {2,}", "  ", t)
    return t.strip()


def _first_match(texts: list[str], pattern: str, group: int = 1) -> Optional[str]:
    """Return the first captured group from pattern across all text lines."""
    rx = re.compile(pattern, re.IGNORECASE)
    for t in texts:
        m = rx.search(t)
        if m:
            return m.group(group).strip()
    return None


def _all_matches(texts: list[str], pattern: str, group: int = 1) -> list[str]:
    rx = re.compile(pattern, re.IGNORECASE)
    seen, results = set(), []
    for t in texts:
        m = rx.search(t)
        if m:
            v = m.group(group).strip()
            if v not in seen:
                results.append(v)
                seen.add(v)
    return results


def _kmh_to_ms(kmh: float) -> float:
    return round(kmh / 3.6, 1)


def parse_pv_elite(docx_path: Path) -> list[Expected]:
    """
    Extract ground-truth design parameters from a PV Elite DOCX report.

    Primary source: the first DOCX table (Vessel Design Summary table),
    which has clean, unambiguous values. Secondary source: paragraph text
    for weights and data not captured in the table.

    Returns a list of Expected objects. Fields that cannot be parsed are
    silently omitted (they appear as NOT IN DOCX in the report).
    """
    doc = Document(str(docx_path))
    texts = [_clean(p.text) for p in doc.paragraphs if p.text.strip()]
    results: list[Expected] = []

    def add(name, display, needles, tol=0.03):
        results.append(Expected(name=name, display=display, needles=needles, numeric_tol=tol))

    # ── Table helper ──────────────────────────────────────────────────────────
    def _tbl_find(desc_fragment: str, val_col: int = 3) -> Optional[str]:
        """Return the value cell from the first table row whose DESCRIPTION
        (column 1) contains desc_fragment (case-insensitive)."""
        if not doc.tables:
            return None
        for row in doc.tables[0].rows:
            cells = [c.text.strip() for c in row.cells]
            if len(cells) > 1 and desc_fragment.lower() in cells[1].lower():
                v = cells[val_col] if len(cells) > val_col else ""
                if not v and len(cells) > 2:
                    v = cells[2]
                return v.strip() or None
        return None

    # ── Design Pressure (Internal) → table "Design Internal Pressure" ─────────
    dp_int_str = _tbl_find("Design Internal Pressure")   # e.g. "0.5 + Full of liquid"
    if dp_int_str:
        m = re.search(r"([\d.]+)", dp_int_str)
        if m:
            v = float(m.group(1))
            add("DESIGN PRESSURE (Internal)", f"{v} bar", [v], tol=0.03)

    # ── Design Pressure (External) → table ────────────────────────────────────
    dp_ext_str = _tbl_find("Design External Pressure")
    if dp_ext_str:
        if dp_ext_str.upper() in ("NIL", "N/A", "NA") or re.fullmatch(r"0+", dp_ext_str.strip()):
            add("DESIGN PRESSURE (External)", "NIL", ["NIL"])
        else:
            m = re.search(r"([\d.]+)", dp_ext_str)
            if m:
                v = float(m.group(1))
                add("DESIGN PRESSURE (External)", f"{v} bar", [v], tol=0.05)

    # ── MAWP → table "MAWP (Hot and Corroded)" ────────────────────────────────
    mawp_str = _tbl_find("MAWP (Hot")
    if mawp_str:
        m = re.search(r"([\d.]+)", mawp_str)
        if m:
            v = float(m.group(1))
            add("PRESSURE MAWP (barg)", f"{v}", [v], tol=0.03)

    # ── Design Temperature (MAX/MIN) → table "Design Temperature (Min" ─────────
    dt_str = _tbl_find("Design Temperature (Min")   # "4 / 85"
    if dt_str:
        nums = re.findall(r"-?[\d.]+", dt_str)
        if len(nums) >= 2:
            t_min_v, t_max_v = float(nums[0]), float(nums[1])
            display = f"{t_max_v:.0f} / {t_min_v:.0f} C"
            add("DESIGN TEMPERATURE (MAX/MIN)", display, [t_max_v, t_min_v], tol=0.05)

    # ── Operating Temperature → table ─────────────────────────────────────────
    ot_str = _tbl_find("Operating Temperature")
    if ot_str:
        m = re.search(r"([\d.]+)", ot_str)
        if m:
            v = float(m.group(1))
            add("OPERATING TEMPERATURE (MAX/MIN)", f"{v:.0f} C", [v], tol=0.05)

    # ── Fluid Density → table "Medium Density" ────────────────────────────────
    dens_str = _tbl_find("Medium Density")
    if dens_str:
        m = re.search(r"([\d.]+)", dens_str)
        if m:
            v = float(m.group(1))
            add("FLUID DENSITY", f"{v:.0f}", [v], tol=0.03)

    # ── Capacity → table ──────────────────────────────────────────────────────
    cap_str = _tbl_find("Capacity (Working")
    if cap_str:
        nums = re.findall(r"[\d.]+", cap_str)
        if len(nums) >= 2:
            v1, v2 = float(nums[0]), float(nums[1])
            add("CAPACITY (WORKING/NOMINAL)", f"{v1:.2f} / {v2:.2f}", [v1, v2], tol=0.03)

    # ── Fluid Joint Efficiency → table "Shell Long Seam" value (sub-row of
    #    "Joint Efficiency" section); needle is the fraction form only ──────────
    je_str = _tbl_find("Shell Long Seam")
    if je_str:
        m = re.search(r"([\d.]+)", je_str)
        if m:
            v = float(m.group(1))
            display = str(int(v)) if v == int(v) else str(v)
            add("FLUID JOINT EFFICIENCY", display, [v], tol=0.01)

    # ── Radiography → description of table row 23 contains "(RT-N)" ───────────
    if doc.tables:
        for row in doc.tables[0].rows:
            cells = [c.text.strip() for c in row.cells]
            if len(cells) > 1:
                m = re.search(r"Radiograph\w*\s*\((RT-?\d+\w*)\)", cells[1], re.I)
                if m:
                    rad = m.group(1).upper()
                    add("RADIOGRAPHY", rad, [rad])
                    break

    # ── Wind Design / Speed → table "Wind Load" (merged cell in col 2) ────────
    wind_str = _tbl_find("Wind Load", val_col=2)
    if wind_str:
        code_m  = re.search(r"ASCE[-\s/]+0?7[-\s]+\(?(\d{4})\)?", wind_str, re.I)
        speed_m = re.search(r"([\d.]+)\s*m/", wind_str)
        if speed_m:
            ms = float(speed_m.group(1))
            year = code_m.group(1) if code_m else ""
            code_disp = f"ASCE-07({year})" if year else "ASCE-07"
            add("WIND DESIGN / SPEED", f"{code_disp} / {ms} m/s", ["ASCE", ms], tol=0.02)

    # ── Seismic Design → table "Seismic Load" (merged cell in col 2) ──────────
    seismic_str = _tbl_find("Seismic Load", val_col=2)
    if seismic_str:
        ss_m = re.search(r"Ss\s*=\s*([\d.]+)", seismic_str)
        s1_m = re.search(r"S1\s*=\s*([\d.]+)", seismic_str)
        needles: list = ["ASCE"]
        if ss_m:
            needles.append(float(ss_m.group(1)))
        if s1_m:
            needles.append(float(s1_m.group(1)))
        display = seismic_str.replace("\n", " ").strip()[:80]
        add("CLIMATIC DATA SEISMIC DESIGN", display, needles, tol=0.05)

    # ── Hydrotest Pressure (Shop / Field) → table ─────────────────────────────
    tp_str = _tbl_find("Hydrotest Pressure at Top")   # "1.035 / 0.744"
    if tp_str:
        nums = re.findall(r"[\d.]+", tp_str)
        if len(nums) >= 2:
            sv, fv = float(nums[0]), float(nums[1])
            add("TEST (SHOP/FIELD)", f"{sv:.3f} / {fv:.3f} bar", [sv, fv], tol=0.03)

    # ── Hydrotest Metal Temperature (MIN./MAX.) → table ───────────────────────
    ht_temp_str = _tbl_find("Metal Temperature during Hydrotest")   # "17/45"
    if ht_temp_str:
        nums = re.findall(r"[\d.]+", ht_temp_str)
        if len(nums) >= 2:
            t1, t2 = float(nums[0]), float(nums[1])
            add("TESTING HYDROTEST METAL TEMPERATURE (MIN./MAX.)",
                f"{t1:.0f} / {t2:.0f} C", [t1, t2], tol=0.05)

    # ── Empty / Erected Weight → paragraph (specific "Erected" line) ──────────
    empty_w = _first_match(
        texts,
        r"Erected\s*-\s*Fab\..*?([\d.]+)\s*kg"
    )
    if empty_w:
        v = float(empty_w)
        add("EMPTY/ERECTED", f"{v:.0f} kg", [v], tol=0.03)

    # ── Operating Weight → paragraph (specific line with "Uncorroded") ─────────
    op_w = _first_match(
        texts,
        r"Operating\s+Wt\.\s*-\s*Empty\s+Weight.*?Uncorroded\s+([\d.]+)\s*kg"
    )
    if op_w:
        v = float(op_w)
        add("OPERATING", f"{v:.1f} kg", [v], tol=0.03)

    return results


# ==============================================================================
# SECTION 3 — MATCHING
# ==============================================================================

_NUMBER_RE = re.compile(r"[\d]+(?:[.,]\d+)?")


def _norm(s: str) -> str:
    return html.unescape(s).lower().replace(",", ".").strip()


def _numbers(s: str) -> list[float]:
    return [float(m.replace(",", ".")) for m in _NUMBER_RE.findall(s)]


def _num_close(target: float, candidates: list[float], tol: float) -> bool:
    if target == 0:
        return any(abs(c) < 1e-9 for c in candidates)
    return any(abs(c - target) / abs(target) <= tol for c in candidates)


def _value_matches(needle, haystack_str: str, tol: float) -> bool:
    """Check if a single needle (str or float) matches anywhere in haystack_str."""
    h_norm = _norm(haystack_str)
    if isinstance(needle, float):
        h_nums = _numbers(h_norm)
        return _num_close(needle, h_nums, tol)
    # string needle: substring match (case-insensitive, normalized)
    return _norm(needle) in h_norm


def validate(expected_list: list[Expected], all_values: list[str]) -> list[Result]:
    """
    For each Expected, check whether ALL its needles are found somewhere
    in the entire extracted VALUE column (whole-column search).
    """
    results = []

    for exp in expected_list:
        # Collect which CAD values actually contained a needle match
        matched_cells = []
        all_passed = True

        for needle in exp.needles:
            needle_found = False
            for v in all_values:
                if _value_matches(needle, v, exp.numeric_tol):
                    needle_found = True
                    if v not in matched_cells:
                        matched_cells.append(v)
            if not needle_found:
                all_passed = False
                break

        status = "PASS" if all_passed else "FAIL"
        matched_str = " ; ".join(matched_cells[:3]) if matched_cells else "(not found)"
        results.append(Result(
            name=exp.name,
            expected_display=exp.display,
            matched_value=matched_str,
            status=status,
        ))

    return results


# ==============================================================================
# SECTION 4 — REPORTING
# ==============================================================================

def _console_report(results: list[Result], docx_name: str, csv_name: str) -> None:
    W = 110
    print("=" * W)
    print("  VALIDATION REPORT")
    print(f"  DOCX : {docx_name}")
    print(f"  CSV  : {csv_name}")
    print(f"  Time : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * W)

    col_w = [38, 28, 28, 8]
    hdr = (f"{'FIELD':<{col_w[0]}}  {'EXPECTED (DOCX)':<{col_w[1]}}"
           f"  {'FOUND IN CAD':<{col_w[2]}}  {'RESULT'}")
    print(hdr)
    print("-" * W)

    for r in results:
        line = (
            f"{r.name[:col_w[0]]:<{col_w[0]}}  "
            f"{r.expected_display[:col_w[1]]:<{col_w[1]}}  "
            f"{r.matched_value[:col_w[2]]:<{col_w[2]}}  "
            f"{r.status}"
        )
        print(line)

    print("=" * W)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    no_doc = sum(1 for r in results if r.status == "NOT IN DOCX")
    total  = len(results)
    pct    = f"{100*passed//total}%" if total else "—"
    print(f"  SUMMARY: {passed} PASS  /  {failed} FAIL  /  {no_doc} NOT IN DOCX  "
          f"({total} total, {pct} pass rate)")
    print("=" * W)


def _write_csv(results: list[Result], path: Path) -> None:
    rows = [
        {"FIELD": r.name, "EXPECTED (DOCX)": r.expected_display,
         "FOUND IN CAD": r.matched_value, "RESULT": r.status}
        for r in results
    ]
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    print(f"[Report] CSV  -> {path}")


def _write_html(results: list[Result], path: Path,
                docx_name: str, csv_name: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    total  = len(results)

    css = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5;
       color: #222; font-size: 13px; padding: 20px; }
h1 { background: linear-gradient(135deg, #1F3864 0%, #2E75B6 100%);
     color: white; padding: 18px 24px; border-radius: 8px 8px 0 0;
     font-size: 1.45em; margin-bottom: 4px; }
.subtitle { background: #2E75B6; color: rgba(255,255,255,0.9); padding: 8px 24px;
            border-radius: 0 0 8px 8px; font-size: 0.9em; margin-bottom: 20px; }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr));
              gap: 12px; margin-bottom: 20px; }
.stat-box { background: white; border-radius: 10px; padding: 16px 12px;
            text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,.08);
            border-top: 3px solid #2E75B6; }
.stat-box .value { font-size: 2.2em; font-weight: 700; color: #1F3864; }
.stat-box .label { font-size: 0.78em; color: #666; margin-top: 5px; }
.card { background: white; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,.09);
        padding: 20px 24px; margin-bottom: 20px; }
h2 { background: #2E75B6; color: white; padding: 9px 16px; margin: -20px -24px 16px;
     border-radius: 10px 10px 0 0; font-size: 1.05em; }
table { border-collapse: collapse; width: 100%; font-size: 11.5px; }
th { background: #1F3864; color: white; padding: 9px 8px; text-align: left; }
td { padding: 7px 8px; border-bottom: 1px solid #eee; vertical-align: top; }
tr:hover td { background: #f5f7ff; }
.pass { color: #375623; font-weight: bold; }
.fail { color: #C00000; font-weight: bold; }
.nodoc { color: #888; font-weight: bold; }
.table-scroll { overflow-x: auto; }
footer { text-align: center; color: #aaa; font-size: 0.78em; margin-top: 20px; }
"""

    rows_html = ""
    for r in results:
        cls = {"PASS": "pass", "FAIL": "fail"}.get(r.status, "nodoc")
        rows_html += (
            f"<tr>"
            f"<td style='font-weight:bold;color:#1F3864'>{r.name}</td>"
            f"<td>{r.expected_display}</td>"
            f"<td>{r.matched_value}</td>"
            f"<td class='{cls}'>{r.status}</td>"
            f"</tr>\n"
        )

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Validation Report</title>
  <style>{css}</style>
</head>
<body>
<h1>&#x2705; Design Data Validation Report</h1>
<div class="subtitle">{docx_name}</div>

<div class="stats-grid">
  <div class="stat-box">
    <div class="value" style="color:#375623">{passed}</div>
    <div class="label">PASS</div>
  </div>
  <div class="stat-box">
    <div class="value" style="color:#C00000">{failed}</div>
    <div class="label">FAIL</div>
  </div>
  <div class="stat-box">
    <div class="value">{total}</div>
    <div class="label">Total Checks</div>
  </div>
</div>

<div class="card">
  <h2>&#x1F4CB; Validation Results</h2>
  <p style="font-size:11px;color:#666;margin-bottom:12px">
    Extracted from: <code>{csv_name}</code> &nbsp;|&nbsp; Generated: {now}
  </p>
  <div class="table-scroll">
    <table>
      <thead><tr><th>FIELD</th><th>EXPECTED (DOCX)</th><th>FOUND IN CAD</th><th>RESULT</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>
<footer>Design Data Validation Pipeline &nbsp;|&nbsp; {now}</footer>
</body>
</html>"""

    path.write_text(html_out, encoding="utf-8")
    print(f"[Report] HTML -> {path}")


# ==============================================================================
# SECTION 5 — MAIN
# ==============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="validate_design_data",
        description="Cross-check CAD-extracted design data against a PV Elite DOCX report.",
    )
    parser.add_argument(
        "--docx", default=DEFAULT_DOCX, metavar="FILE",
        help=f"PV Elite DOCX report (default: {DEFAULT_DOCX})",
    )
    parser.add_argument(
        "--csv", default=DEFAULT_CSV, metavar="FILE",
        help=f"Extracted design data CSV (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--outdir", default=DEFAULT_OUT, metavar="DIR",
        help=f"Directory for output report files (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args()

    docx_path = Path(args.docx)
    csv_path  = Path(args.csv)
    outdir    = Path(args.outdir)

    if not docx_path.exists():
        print(f"[ERROR] DOCX not found: {docx_path}", file=sys.stderr)
        return 1
    if not csv_path.exists():
        print(f"[ERROR] CSV not found: {csv_path}", file=sys.stderr)
        return 1

    outdir.mkdir(parents=True, exist_ok=True)

    print(f"[Step 1] Parsing PV Elite report: {docx_path.name} ...")
    try:
        expected_list = parse_pv_elite(docx_path)
    except Exception as e:
        print(f"[ERROR] Failed to parse DOCX: {e}", file=sys.stderr)
        return 1
    print(f"[Step 1] Extracted {len(expected_list)} ground-truth parameters.")

    print(f"[Step 2] Loading extracted CAD values: {csv_path.name} ...")
    try:
        _rows, all_values = load_extracted_values(csv_path)
    except Exception as e:
        print(f"[ERROR] Failed to read CSV: {e}", file=sys.stderr)
        return 1
    print(f"[Step 2] Loaded {len(all_values)} extracted values.")

    print("[Step 3] Running validation ...")
    results = validate(expected_list, all_values)

    _console_report(results, docx_path.name, csv_path.name)

    _write_csv(results, outdir / "validation_report.csv")
    _write_html(results, outdir / "validation_report.html",
                docx_path.name, csv_path.name)

    failed = sum(1 for r in results if r.status == "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
