#!/usr/bin/env python3
"""
design_pipeline.py
==================
Extract three engineering tables from a DWG/DXF drawing:
  1. DESIGN DATA           — key/value vessel parameters
  2. MATERIAL SPECIFICATION — component description → material grade
  3. FOUNDATION LOAD DATA  — vessel weights + wind/seismic shear & moment

Usage
-----
  python design_pipeline.py drawing.dwg
  python design_pipeline.py drawing.dwg --outdir ./reports
  python design_pipeline.py drawing.dwg --debug
  python design_pipeline.py drawing.dwg --project "My Project"

Dependencies
------------
  pip install ezdxf pandas openpyxl matplotlib

ODA Fallback (optional, only if ezdxf cannot open the DWG directly)
  Download ODA File Converter from:
  https://www.opendesign.com/guestfiles/oda_file_converter
  Ensure the executable is on PATH as "ODAFileConverter".
"""

import argparse
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd


# ==============================================================================
# SHARED CONSTANTS
# ==============================================================================

MIN_TEXT_HEIGHT = 0.5    # drawing units — filters out tiny dimension tick text
MAX_TEXT_HEIGHT = 200.0

_ACAD_CODE_RE = re.compile(r"%%(?:[a-zA-Z]|\d{3})", re.IGNORECASE)

# Anchor detection patterns for each table
DESIGN_DATA_PATTERN     = re.compile(r"\bDESIGN\s+DATA\b",    re.IGNORECASE)
MATERIAL_SPEC_PATTERN   = re.compile(r"MATERIAL\s+SPEC",       re.IGNORECASE)
FOUNDATION_LOAD_PATTERN = re.compile(r"FOUNDATION\s+LOAD",     re.IGNORECASE)

DEFAULT_PROJECT = "ADNOC/KNPC - Pressure Vessel Design"
DEFAULT_OUTPUT  = "design_report.html"


# ==============================================================================
# SECTION 1 — DWG / DXF LOADING
# ==============================================================================

_CONVERTER_HELP = """
──────────────────────────────────────────────────────────────────
  ezdxf could not open the DWG file (likely an older format).
  Install ONE of the following free converters, then re-run:

  Option A  ODA File Converter  (recommended for Windows)
    https://www.opendesign.com/guestfiles/oda_file_converter
    Binary name: "ODAFileConverter"

  Option B  LibreDWG
    Windows : https://github.com/LibreDWG/libredwg/releases
    Linux   : sudo apt install libredwg-utils
    macOS   : brew install libredwg
──────────────────────────────────────────────────────────────────
"""


def _strip_acad_codes(text: str) -> str:
    """Remove AutoCAD control codes like %%U, %%O, %%D."""
    return _ACAD_CODE_RE.sub("", text).strip()


def _oda_convert(dwg_path: Path) -> Path:
    """Convert DWG to DXF using ODA File Converter."""
    print("[Step 1] Trying ODA File Converter fallback ...")
    tmp_dir = tempfile.mkdtemp(prefix="oda_out_")
    cmd = [
        "ODAFileConverter",
        str(dwg_path.parent), tmp_dir,
        "ACAD2018", "DXF", "0", "1",
        str(dwg_path.name),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"ODA returned non-zero: {result.stderr.strip()}")
    except FileNotFoundError:
        raise RuntimeError("ODAFileConverter not found — ODA is not installed or not on PATH.")
    dxf_path = Path(tmp_dir) / dwg_path.with_suffix(".dxf").name
    if not dxf_path.exists():
        candidates = list(Path(tmp_dir).glob("*.dxf"))
        if not candidates:
            raise RuntimeError(f"ODA produced no DXF in {tmp_dir}")
        dxf_path = candidates[0]
    print(f"[Step 1] ODA conversion OK -> {dxf_path}")
    return dxf_path


def _libredwg_convert(dwg_path: Path) -> Path:
    """Convert DWG to DXF using LibreDWG's dwg2dxf CLI."""
    print("[Step 1] Trying LibreDWG (dwg2dxf) fallback ...")
    tmp_dir = Path(tempfile.mkdtemp(prefix="libredwg_out_"))
    out_dxf = tmp_dir / dwg_path.with_suffix(".dxf").name
    cmd = ["dwg2dxf", str(dwg_path), "-o", str(out_dxf)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"dwg2dxf failed (exit {result.returncode}): {result.stderr.strip()}")
    except FileNotFoundError:
        raise RuntimeError("dwg2dxf not found — LibreDWG is not installed or not on PATH.")
    if not out_dxf.exists():
        candidates = list(tmp_dir.glob("*.dxf"))
        if not candidates:
            raise RuntimeError(f"dwg2dxf produced no DXF in {tmp_dir}")
        out_dxf = candidates[0]
    print(f"[Step 1] LibreDWG conversion OK -> {out_dxf}")
    return out_dxf


def load_document(file_path: Path):
    """
    Load a DWG or DXF with ezdxf.
    Fallback chain: ODA File Converter → LibreDWG.
    Returns an ezdxf document object.
    """
    from ezdxf import recover
    suffix = file_path.suffix.lower()
    print(f"[Step 1] Loading '{file_path}' ...")
    try:
        doc, auditor = recover.readfile(str(file_path))
        if auditor.has_errors:
            print(f"[Step 1] Audit found {len(auditor.errors)} error(s) — continuing anyway.")
        print("[Step 1] File loaded OK.")
        return doc
    except Exception as exc:
        print(f"[Step 1] ezdxf could not open '{file_path}': {exc}")
        if suffix != ".dwg":
            raise
        for converter_fn, label in [(_oda_convert, "ODA"), (_libredwg_convert, "LibreDWG")]:
            try:
                dxf_path = converter_fn(file_path)
                doc, auditor = recover.readfile(str(dxf_path))
                print(f"[Step 1] {label}-converted DXF loaded OK.")
                return doc
            except RuntimeError as cerr:
                print(f"[Step 1] {label} not available: {cerr}")
            except Exception as cerr:
                print(f"[Step 1] {label} conversion failed: {cerr}")
        print(_CONVERTER_HELP)
        raise RuntimeError("No DWG converter found. See installation options printed above.")


# ==============================================================================
# SECTION 2 — TEXT ENTITY COLLECTION
# ==============================================================================

def _get_insert_xy(entity) -> tuple[float, float]:
    """Return the (x, y) insert point of a TEXT or MTEXT entity."""
    etype = entity.dxftype()
    if etype == "TEXT":
        pt = entity.dxf.align_point if entity.dxf.hasattr("align_point") else entity.dxf.insert
        return float(pt.x), float(pt.y)
    elif etype == "MTEXT":
        pt = entity.dxf.insert
        return float(pt.x), float(pt.y)
    return 0.0, 0.0


def _get_text_content(entity) -> str:
    """Return plain text content, stripping MTEXT formatting codes."""
    etype = entity.dxftype()
    if etype == "TEXT":
        return (entity.dxf.text or "").strip()
    elif etype == "MTEXT":
        try:
            return entity.plain_text().strip()
        except AttributeError:
            try:
                return entity.plain_mtext().strip()
            except Exception:
                return (entity.dxf.text or "").strip()
        except Exception:
            return (entity.dxf.text or "").strip()
    return ""


def collect_all_entities(doc) -> list[dict]:
    """Collect all TEXT/MTEXT entities from ALL layers, with height filtering."""
    msp = doc.modelspace()
    result = []
    for ent in msp:
        if ent.dxftype() not in ("TEXT", "MTEXT"):
            continue
        x, y = _get_insert_xy(ent)
        content = _get_text_content(ent)
        if not content:
            continue
        attr = "height" if ent.dxftype() == "TEXT" else "char_height"
        h = float(ent.dxf.get(attr, 0) or 0)
        if MIN_TEXT_HEIGHT <= h <= MAX_TEXT_HEIGHT:
            result.append({
                "x": x, "y": y, "text": content, "height": h,
                "layer": ent.dxf.layer,
            })
    print(f"[Step 2] Collected {len(result)} text entities across all layers.")
    return result


# ==============================================================================
# SECTION 3 — SHARED UTILITIES
# ==============================================================================

def _cluster_1d(values: list[float], tolerance: float) -> list[list[int]]:
    """Group indices of `values` into clusters within tolerance."""
    if not values:
        return []
    indexed = sorted(enumerate(values), key=lambda t: t[1])
    clusters: list[list[int]] = []
    current = [indexed[0][0]]
    current_val = indexed[0][1]
    for idx, val in indexed[1:]:
        if abs(val - current_val) <= tolerance:
            current.append(idx)
        else:
            clusters.append(current)
            current = [idx]
            current_val = val
    clusters.append(current)
    return clusters


def _median(values: list[float]) -> float:
    if not values:
        return 1.0
    s = sorted(values)
    return s[len(s) // 2]


def _find_anchor(entities: list[dict], pattern: re.Pattern, label: str) -> Optional[dict]:
    """
    Find the best anchor entity (largest height) matching the given pattern.
    Returns None and prints a notice if the table is not found.
    """
    candidates = [e for e in entities if pattern.search(_strip_acad_codes(e["text"]))]
    if not candidates:
        print(f"[Step 3] '{label}' — table not found in this drawing.")
        return None
    best = max(candidates, key=lambda e: e["height"])
    print(f"[Step 3] '{label}' anchor: '{_strip_acad_codes(best['text'])}' "
          f"@ ({best['x']:.1f}, {best['y']:.1f}) h={best['height']:.2f}")
    return best


def _get_roi(entities: list[dict], anchor: dict,
             xl: float, xr: float, ya: float, yb: float) -> list[dict]:
    """
    Return entities within a proportional bounding box around the anchor.
    xl/xr/ya/yb are multiples of anchor text height.
    """
    ax, ay, h = anchor["x"], anchor["y"], anchor["height"]
    return [e for e in entities
            if (ax - xl * h) <= e["x"] <= (ax + xr * h)
            and (ay - yb * h) <= e["y"] <= (ay + ya * h)]


def _rows_from_entities(ents: list[dict]) -> list[dict]:
    """Y-cluster entities into rows. Returns [{texts, xs, y, ents}] top-to-bottom."""
    if not ents:
        return []
    ref_h = _median([e["height"] for e in ents])
    y_tol = 0.55 * ref_h
    ys = [e["y"] for e in ents]
    clusters = _cluster_1d(ys, y_tol)
    clusters.sort(key=lambda c: -_median([ys[i] for i in c]))
    rows = []
    for c in clusters:
        row_ents = sorted([ents[i] for i in c], key=lambda e: e["x"])
        texts = [_strip_acad_codes(e["text"]) for e in row_ents]
        xs    = [e["x"] for e in row_ents]
        mean_y = _median([e["y"] for e in row_ents])
        rows.append({"texts": texts, "xs": xs, "y": mean_y, "ents": row_ents})
    return rows


def _to_float(s) -> Optional[float]:
    """Parse a string to float; return None for dashes or invalid text."""
    if s is None:
        return None
    s = str(s).strip()
    if s in ("-", "—", "–", "N/A", "NA", ""):
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _cluster_into_columns(ents: list[dict]) -> list[list[int]]:
    """
    Cluster entity X positions into distinct columns.
    Uses 2× median height as tolerance, then falls back to 5× if < 2 clusters found.
    Returns clusters sorted left-to-right.
    """
    if not ents:
        return []
    ref_h = _median([e["height"] for e in ents])
    xs = [e["x"] for e in ents]
    for tol_factor in [2, 5, 10]:
        clusters = _cluster_1d(xs, tolerance=tol_factor * ref_h)
        if len(clusters) >= 2:
            clusters.sort(key=lambda c: _median([xs[i] for i in c]))
            return clusters
    clusters = _cluster_1d(xs, tolerance=tol_factor * ref_h)
    clusters.sort(key=lambda c: _median([xs[i] for i in c]))
    return clusters


def _y_pair_rows(left_ents: list[dict], right_ents: list[dict],
                 max_y_dist_factor: float = 10.0) -> list[tuple[str, str]]:
    """
    Pair left-column and right-column entities by Y-proximity.
    Returns list of (left_text, right_text) pairs.
    """
    if not left_ents or not right_ents:
        return []

    ref_h = _median([e["height"] for e in left_ents + right_ents])

    def cluster_col(ents) -> list[dict]:
        h = _median([e["height"] for e in ents])
        ys = [e["y"] for e in ents]
        clusters = _cluster_1d(ys, 0.8 * h)
        clusters.sort(key=lambda c: -_median([ys[i] for i in c]))
        rows = []
        for c in clusters:
            row_ents = sorted([ents[i] for i in c], key=lambda e: e["x"])
            texts = [_strip_acad_codes(e["text"]) for e in row_ents
                     if _strip_acad_codes(e["text"])]
            if texts:
                rows.append({
                    "text": " ".join(texts),
                    "y": _median([e["y"] for e in row_ents]),
                })
        return rows

    left_rows  = cluster_col(left_ents)
    right_rows = cluster_col(right_ents)

    pairs = []
    used  = set()
    rys   = [r["y"] for r in right_rows]

    for lr in left_rows:
        candidates = [(abs(ry - lr["y"]), i) for i, ry in enumerate(rys) if i not in used]
        if not candidates:
            break
        dist, best_i = min(candidates)
        if dist <= max_y_dist_factor * ref_h:
            pairs.append((lr["text"], right_rows[best_i]["text"]))
            used.add(best_i)

    return pairs


# ==============================================================================
# SECTION 4 — DESIGN DATA EXTRACTION
# ==============================================================================

def extract_design_data(entities: list[dict]) -> pd.DataFrame:
    """
    Extract the DESIGN DATA table as a list of (FIELD, VALUE) pairs.

    Table layout (two or three X-clusters):
      Section headers  |  Field labels  |  Values
       (tall text)     |  (medium text) |  (medium text)

    Strategy: cluster columns by X → rightmost = values, second = labels.
    Then filter out tall section-header text from the label cluster.
    Pair labels ↔ values by nearest Y.
    """
    anchor = _find_anchor(entities, DESIGN_DATA_PATTERN, "Design Data")
    if anchor is None:
        return pd.DataFrame(columns=["FIELD", "VALUE"])

    ax, ay = anchor["x"], anchor["y"]
    ref_h  = anchor["height"]

    # ROI: tight horizontal bounds (labels are left, values are around/right of anchor)
    roi = _get_roi(entities, anchor, xl=40, xr=15, ya=5, yb=65)
    # Exclude entities at/above the anchor row and the anchor itself
    roi = [e for e in roi
           if e["y"] <= ay + 0.5 * ref_h
           and not (DESIGN_DATA_PATTERN.search(_strip_acad_codes(e["text"]))
                    and e["height"] >= 0.8 * ref_h)]

    # Extract labels and values based on X relative to anchor
    label_ents = [e for e in roi if (ax - 26 * ref_h) <= e["x"] <= (ax - 5 * ref_h)]
    value_ents = [e for e in roi if (ax - 5 * ref_h) < e["x"] <= (ax + 15 * ref_h)]

    if not label_ents or not value_ents:
        print("[Design Data] Could not find label/value columns.")
        return pd.DataFrame(columns=["FIELD", "VALUE"])

    # Within the label cluster, discard "section header" entities that are
    # noticeably taller than the bulk of label text (e.g. CODE, WEIGHTS, etc.)
    if label_ents:
        label_h_median = _median([e["height"] for e in label_ents])
        label_ents = [e for e in label_ents if e["height"] <= 1.6 * label_h_median]

    pairs = _y_pair_rows(label_ents, value_ents, max_y_dist_factor=12.0)

    # Drop obvious non-data rows (section dividers that leaked through)
    _SECTION_HDR = re.compile(
        r"^(PRESSURE|TEMPERATURE|WEIGHTS?|CODE|FLUID|INSPECTION|PAIN\w*|"
        r"SHOP\s+TESTING|CLIMATIC\s+DATA|TEMPER\w*|APPROX\w*)$",
        re.IGNORECASE,
    )
    records = [{"FIELD": f, "VALUE": v} for f, v in pairs
               if not _SECTION_HDR.match(f.strip())]

    df = pd.DataFrame(records, columns=["FIELD", "VALUE"])
    print(f"[Design Data] Extracted {len(df)} key-value pairs.")
    return df


# ==============================================================================
# SECTION 5 — MATERIAL SPECIFICATION EXTRACTION
# ==============================================================================

def extract_material_spec(entities: list[dict]) -> pd.DataFrame:
    """
    Extract the MATERIAL SPECIFICATION table: DESCRIPTION | MATERIAL.

    The table is a clean 2-column grid directly below the anchor title.
    """
    anchor = _find_anchor(entities, MATERIAL_SPEC_PATTERN, "Material Specification")
    if anchor is None:
        return pd.DataFrame(columns=["DESCRIPTION", "MATERIAL"])

    ax, ay = anchor["x"], anchor["y"]
    ref_h  = anchor["height"]

    # ROI: tight horizontal window, below anchor
    roi = _get_roi(entities, anchor, xl=35, xr=15, ya=3, yb=25)
    roi = [e for e in roi if e["y"] < ay - 0.2 * ref_h]

    if len(roi) < 4:
        print("[Material Spec] Insufficient entities in ROI.")
        return pd.DataFrame(columns=["DESCRIPTION", "MATERIAL"])

    # Extract descriptions and materials based on X relative to anchor
    desc_ents = [e for e in roi if (ax - 26 * ref_h) <= e["x"] <= (ax - 5 * ref_h)]
    mat_ents  = [e for e in roi if (ax - 5 * ref_h) < e["x"] <= (ax + 15 * ref_h)]

    pairs = _y_pair_rows(desc_ents, mat_ents, max_y_dist_factor=8.0)

    # Filter out header rows (those containing "DESCRIPTION" or "MATERIAL")
    _HDR_RE = re.compile(r"\b(DESCRIPTION|MATERIAL)\b", re.IGNORECASE)
    records = [{"DESCRIPTION": d, "MATERIAL": m}
               for d, m in pairs
               if not _HDR_RE.search(d) and not _HDR_RE.search(m)]

    df = pd.DataFrame(records, columns=["DESCRIPTION", "MATERIAL"])
    print(f"[Material Spec] Extracted {len(df)} rows.")
    return df


# ==============================================================================
# SECTION 6 — FOUNDATION LOAD DATA EXTRACTION
# ==============================================================================

def _parse_weight_row(rows: list[dict]) -> pd.DataFrame:
    """
    Find and parse the weight data row (4 large integer values in kgf).
    Returns a single-row DataFrame.
    """
    cols = ["FABRICATED_WEIGHT_kgf", "EMPTY_WEIGHT_kgf",
            "OPERATING_WEIGHT_kgf", "HYDROTEST_WEIGHT_kgf"]
    empty = pd.DataFrame(columns=cols)

    weight_kw = re.compile(r"\b(FABRICAT|EMPTY|OPERAT|HYDROTEST|WEIGHT)\b", re.IGNORECASE)

    # Find Y of weight header rows
    header_y = None
    for row in rows:
        if any(weight_kw.search(t) for t in row["texts"]):
            header_y = row["y"]
            break

    if header_y is None:
        return empty

    # The data row is the first row below headers with ≥ 3 large integers (≥ 100)
    for row in rows:
        if row["y"] >= header_y:
            continue
        nums = [_to_float(t) for t in row["texts"]]
        large = [v for v in nums if v is not None and v >= 100]
        if len(large) >= 3:
            # Sort by X → left-to-right = FABRICATED, EMPTY, OPERATING, HYDROTEST
            vals_sorted = sorted(
                [(x, _to_float(t)) for x, t in zip(row["xs"], row["texts"])
                 if _to_float(t) is not None],
                key=lambda p: p[0],
            )
            numeric = [v for _, v in vals_sorted]
            record = {col: numeric[i] if i < len(numeric) else None
                      for i, col in enumerate(cols)}
            return pd.DataFrame([record])

    return empty


def _parse_shear_moment(rows: list[dict]) -> pd.DataFrame:
    """
    Find and parse the shear & moment data row.
    Detects WIND/SEISMIC split from header text X positions,
    then assigns OPE./EMPTY/TEST via condition sub-header X positions.
    Returns a DataFrame with CONDITION | WIND_SHEAR_kN | WIND_MOMENT_kNm | ...
    """
    cols = ["CONDITION", "WIND_SHEAR_kN", "WIND_MOMENT_kNm",
            "SEISMIC_SHEAR_kN", "SEISMIC_MOMENT_kNm"]
    empty = pd.DataFrame(columns=cols)

    # ── Find the numeric data row ──────────────────────────────────────────────
    # A shear/moment row has ≥ 8 entries that are either floats or dashes,
    # and NO large integers (which would indicate a weight row).
    data_row = None
    for row in rows:
        all_v = [_to_float(t) for t in row["texts"]]
        dash_or_float = sum(
            1 for v, t in zip(all_v, row["texts"])
            if v is not None or t.strip() in ("-", "—", "–")
        )
        float_count = sum(1 for v in all_v if v is not None)
        large_count = sum(1 for v in all_v if v is not None and v >= 100)
        if dash_or_float >= 8 and float_count >= 4 and large_count == 0:
            data_row = row
            break

    if data_row is None:
        return empty

    # ── Find WIND/SEISMIC X boundary ───────────────────────────────────────────
    wind_xs, seismic_xs = [], []
    for row in rows:
        for t, x in zip(row["texts"], row["xs"]):
            tu = t.strip().upper()
            if tu == "WIND":
                wind_xs.append(x)
            if "SEISMIC" in tu or "EARTHQUAKE" in tu:
                seismic_xs.append(x)

    data_xs = data_row["xs"]
    data_xs_sorted = sorted(data_xs)
    n = len(data_xs_sorted)

    if wind_xs and seismic_xs:
        wind_centroid    = _median(wind_xs)
        seismic_centroid = _median(seismic_xs)
        x_boundary = (wind_centroid + seismic_centroid) / 2
    else:
        # Fallback: split data columns in half
        x_boundary = data_xs_sorted[n // 2]

    wind_data    = [(x, t) for x, t in zip(data_row["xs"], data_row["texts"]) if x <  x_boundary]
    seismic_data = [(x, t) for x, t in zip(data_row["xs"], data_row["texts"]) if x >= x_boundary]

    # ── Within wind/seismic, split shear vs moment at the largest X gap ────────
    def split_shear_moment(xv_pairs):
        if len(xv_pairs) < 2:
            return xv_pairs, []
        xs_only = sorted(x for x, _ in xv_pairs)
        if len(xs_only) < 2:
            return xv_pairs, []
        gaps = [(xs_only[i+1] - xs_only[i], xs_only[i], xs_only[i+1])
                for i in range(len(xs_only) - 1)]
        _, xl, xr = max(gaps, key=lambda g: g[0])
        mid = (xl + xr) / 2
        shear  = [(x, t) for x, t in xv_pairs if x <= mid]
        moment = [(x, t) for x, t in xv_pairs if x >  mid]
        return shear, moment

    wind_shear,    wind_moment    = split_shear_moment(wind_data)
    seismic_shear, seismic_moment = split_shear_moment(seismic_data)

    # ── Assign OPE./EMPTY/TEST conditions via sub-header labels ────────────────
    cond_re = re.compile(r"\b(OPE\.?|OPERAT\w*|EMPTY|TEST)\b", re.IGNORECASE)
    cond_map = []   # list of (x, "OPE." | "EMPTY" | "TEST")
    for row in rows:
        count = sum(1 for t in row["texts"] if cond_re.search(t))
        if count >= 3:   # this is the condition sub-header row
            for t, x in zip(row["texts"], row["xs"]):
                tu = t.strip().upper().rstrip(".")
                if tu.startswith("OPE"):
                    cond_map.append((x, "OPE."))
                elif "EMPTY" in tu:
                    cond_map.append((x, "EMPTY"))
                elif "TEST" in tu:
                    cond_map.append((x, "TEST"))
            break

    def get_cond(x_pos):
        if not cond_map:
            return None
        return min(cond_map, key=lambda cm: abs(cm[0] - x_pos))[1]

    def pairs_to_dict(xv_pairs):
        result = {}
        for x, t in xv_pairs:
            cond = get_cond(x)
            if cond:
                result[cond] = _to_float(t)
        return result

    ws_d = pairs_to_dict(wind_shear)
    wm_d = pairs_to_dict(wind_moment)
    ss_d = pairs_to_dict(seismic_shear)
    sm_d = pairs_to_dict(seismic_moment)

    if cond_map:
        conditions = ["OPE.", "EMPTY", "TEST"]
    else:
        # Fallback: label by position (1st=OPE., 2nd=EMPTY, 3rd=TEST)
        def positional_dict(xv_pairs):
            labels = ["OPE.", "EMPTY", "TEST"]
            return {labels[i]: _to_float(t) for i, (_, t) in enumerate(xv_pairs)
                    if i < len(labels)}
        ws_d = positional_dict(wind_shear)
        wm_d = positional_dict(wind_moment)
        ss_d = positional_dict(seismic_shear)
        sm_d = positional_dict(seismic_moment)
        conditions = ["OPE.", "EMPTY", "TEST"]

    records = [
        {
            "CONDITION":          cond,
            "WIND_SHEAR_kN":      ws_d.get(cond),
            "WIND_MOMENT_kNm":    wm_d.get(cond),
            "SEISMIC_SHEAR_kN":   ss_d.get(cond),
            "SEISMIC_MOMENT_kNm": sm_d.get(cond),
        }
        for cond in conditions
    ]
    return pd.DataFrame(records, columns=cols)


def extract_foundation_loads(entities: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Extract FOUNDATION LOAD DATA into two DataFrames:
      df_weights : vessel weights (FABRICATED / EMPTY / OPERATING / HYDROTEST) in kgf
      df_loads   : shear & moment per condition (OPE. / EMPTY / TEST)
    """
    empty_w = pd.DataFrame(columns=[
        "FABRICATED_WEIGHT_kgf", "EMPTY_WEIGHT_kgf",
        "OPERATING_WEIGHT_kgf",  "HYDROTEST_WEIGHT_kgf"])
    empty_l = pd.DataFrame(columns=[
        "CONDITION", "WIND_SHEAR_kN", "WIND_MOMENT_kNm",
        "SEISMIC_SHEAR_kN", "SEISMIC_MOMENT_kNm"])

    anchor = _find_anchor(entities, FOUNDATION_LOAD_PATTERN, "Foundation Load Data")
    if anchor is None:
        return empty_w, empty_l

    ax, ay = anchor["x"], anchor["y"]
    ref_h  = anchor["height"]

    # ROI: wide horizontal span for multi-column load tables, deep vertically
    roi = _get_roi(entities, anchor, xl=55, xr=65, ya=3, yb=110)
    roi = [e for e in roi if e["y"] < ay]   # only below the anchor title

    if len(roi) < 4:
        print("[Foundation Loads] Insufficient entities in ROI.")
        return empty_w, empty_l

    rows = _rows_from_entities(roi)

    df_weights = _parse_weight_row(rows)
    df_loads   = _parse_shear_moment(rows)

    print(f"[Foundation Loads] Weights: {df_weights.to_dict(orient='records')}")
    print(f"[Foundation Loads] Load conditions: {len(df_loads)} rows.")
    return df_weights, df_loads


# ==============================================================================
# SECTION 7 — DEBUG SCATTER PLOT
# ==============================================================================

def _save_debug_plot(entities: list[dict], file_stem: str, out_path: Path) -> None:
    """Plot all extracted text entities as a spatial scatter for debugging."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[Debug] matplotlib not available — skipping PNG.")
        return

    fig, ax = plt.subplots(figsize=(20, 12))
    ax.scatter([e["x"] for e in entities], [e["y"] for e in entities],
               s=8, alpha=0.5, color="steelblue")
    for e in entities[:400]:
        ax.annotate(e["text"][:18], (e["x"], e["y"]), fontsize=3, alpha=0.7)

    # Highlight anchors
    patterns = [
        (DESIGN_DATA_PATTERN,     "red",    "Design Data"),
        (MATERIAL_SPEC_PATTERN,   "green",  "Material Spec"),
        (FOUNDATION_LOAD_PATTERN, "orange", "Foundation Loads"),
    ]
    for pat, color, label in patterns:
        found = [e for e in entities if pat.search(_strip_acad_codes(e["text"]))]
        if found:
            best = max(found, key=lambda e: e["height"])
            ax.scatter([best["x"]], [best["y"]], color=color, s=200, zorder=5,
                       marker="*", label=label)

    ax.invert_yaxis()
    ax.set_xlabel("X (drawing units)")
    ax.set_ylabel("Y (drawing units)")
    ax.set_title(f"Text entity scatter — {file_stem}")
    ax.legend()
    plt.tight_layout()
    fig.savefig(str(out_path), dpi=120)
    plt.close(fig)
    print(f"[Debug] Scatter plot -> {out_path}")


# ==============================================================================
# SECTION 8 — EXPORT (CSV + XLSX)
# ==============================================================================

def _apply_xlsx_header_style(ws) -> None:
    """Apply blue header styling to the first row of an openpyxl worksheet."""
    from openpyxl.styles import Font, PatternFill, Alignment
    fill = PatternFill(fill_type="solid", fgColor="1F4E79")
    font = Font(bold=True, color="FFFFFF", size=11)
    for cell in ws[1]:
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value is not None), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 55)


def export_all(df_design: pd.DataFrame, df_matspec: pd.DataFrame,
               df_weights: pd.DataFrame, df_loads: pd.DataFrame,
               outdir: Path) -> None:
    """Export all extracted DataFrames as CSV files and a single XLSX workbook."""
    # ── CSV outputs ────────────────────────────────────────────────────────────
    if not df_design.empty:
        p = outdir / "design_data.csv"
        df_design.to_csv(p, index=False, encoding="utf-8")
        print(f"[Export] CSV -> {p}")

    if not df_matspec.empty:
        p = outdir / "material_spec.csv"
        df_matspec.to_csv(p, index=False, encoding="utf-8")
        print(f"[Export] CSV -> {p}")

    if not df_weights.empty:
        p = outdir / "foundation_weights.csv"
        df_weights.to_csv(p, index=False, encoding="utf-8")
        print(f"[Export] CSV -> {p}")

    if not df_loads.empty:
        p = outdir / "foundation_loads.csv"
        df_loads.to_csv(p, index=False, encoding="utf-8")
        print(f"[Export] CSV -> {p}")

    # ── Single XLSX workbook with all sheets ───────────────────────────────────
    xlsx_path = outdir / "design_extraction.xlsx"
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        sheets = []
        if not df_design.empty:
            df_design.to_excel(writer, index=False, sheet_name="Design Data")
            sheets.append("Design Data")
        if not df_matspec.empty:
            df_matspec.to_excel(writer, index=False, sheet_name="Material Spec")
            sheets.append("Material Spec")
        if not df_weights.empty:
            df_weights.to_excel(writer, index=False, sheet_name="Fdn Weights")
            sheets.append("Fdn Weights")
        if not df_loads.empty:
            df_loads.to_excel(writer, index=False, sheet_name="Fdn Shear Moment")
            sheets.append("Fdn Shear Moment")
        for sheet in sheets:
            _apply_xlsx_header_style(writer.sheets[sheet])
    print(f"[Export] XLSX -> {xlsx_path}")


# ==============================================================================
# SECTION 9 — HTML REPORT RENDERER
# ==============================================================================

def _df_to_html_rows(df: pd.DataFrame) -> str:
    """Convert a DataFrame to HTML table rows."""
    if df.empty:
        return '<p style="color:#888;font-style:italic;padding:8px">No data extracted.</p>'
    headers = "".join(f"<th>{col}</th>" for col in df.columns)
    body = ""
    for _, row in df.iterrows():
        cells = ""
        for i, col in enumerate(df.columns):
            val = row[col]
            val_str = "—" if (val is None or (isinstance(val, float) and pd.isna(val))) else str(val)
            style = ' style="font-weight:bold;color:#1F3864;"' if i == 0 else ""
            cells += f"<td{style}>{val_str}</td>"
        body += f"<tr>{cells}</tr>\n"
    return f"""
    <div class="table-scroll">
      <table>
        <thead><tr>{headers}</tr></thead>
        <tbody>{body}</tbody>
      </table>
    </div>"""


def render_html(df_design: pd.DataFrame, df_matspec: pd.DataFrame,
                df_weights: pd.DataFrame, df_loads: pd.DataFrame,
                args) -> str:
    """Render a single consolidated HTML report for all three tables."""
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    project = getattr(args, "project", DEFAULT_PROJECT)
    drawing = getattr(args, "drawing", "—")

    n_design  = len(df_design)
    n_matspec = len(df_matspec)
    n_loads   = len(df_loads)

    status_design  = "✔" if n_design  > 0 else "✘"
    status_matspec = "✔" if n_matspec > 0 else "✘"
    status_loads   = "✔" if n_loads   > 0 else "✘"

    css = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5;
         color: #222; font-size: 13px; padding: 20px; }
  h1 { background: linear-gradient(135deg, #1F3864 0%, #2E75B6 100%);
       color: white; padding: 18px 24px; border-radius: 8px 8px 0 0;
       font-size: 1.45em; letter-spacing: 0.3px; margin-bottom: 4px; }
  .subtitle { background: #2E75B6; color: rgba(255,255,255,0.9); padding: 8px 24px;
              border-radius: 0 0 8px 8px; font-size: 0.9em; margin-bottom: 20px; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr));
                gap: 12px; margin-bottom: 20px; }
  .stat-box { background: white; border-radius: 10px; padding: 16px 12px;
              text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,.08);
              border-top: 3px solid #2E75B6; }
  .stat-box .value { font-size: 2.2em; font-weight: 700; color: #1F3864; }
  .stat-box .label { font-size: 0.78em; color: #666; margin-top: 5px; line-height:1.3; }
  .stat-box .status { font-size: 1.1em; margin-bottom: 4px; }
  .ok  { color: #375623; }
  .bad { color: #C00000; }
  .card { background: white; border-radius: 10px;
          box-shadow: 0 2px 10px rgba(0,0,0,.09);
          padding: 20px 24px; margin-bottom: 20px; }
  h2 { background: #2E75B6; color: white; padding: 9px 16px; margin: -20px -24px 16px;
       border-radius: 10px 10px 0 0; font-size: 1.05em; }
  table { border-collapse: collapse; width: 100%; font-size: 11.5px; }
  th { background: #1F3864; color: white; padding: 9px 8px; text-align: left;
       white-space: nowrap; }
  td { padding: 7px 8px; border-bottom: 1px solid #eee; vertical-align: top; }
  tr:hover td { background: #f5f7ff; }
  .table-scroll { overflow-x: auto; border-radius: 0 0 6px 6px; }
  .meta-grid { display: grid; grid-template-columns: 180px 1fr; gap: 5px 12px; font-size:12px; }
  .meta-label { font-weight: bold; color: #555; }
  footer { text-align: center; color: #aaa; font-size: 0.78em; margin-top: 20px; }
  @media print { body { background: white; } .card { box-shadow: none; border: 1px solid #ddd; } }
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Design Data Extraction Report</title>
  <style>{css}</style>
</head>
<body>
<h1>&#x1F4CB; Design Data Extraction Report</h1>
<div class="subtitle">{project}</div>

<div class="stats-grid">
  <div class="stat-box">
    <div class="status {'ok' if n_design > 0 else 'bad'}">{status_design}</div>
    <div class="value">{n_design}</div>
    <div class="label">Design Data<br>Fields</div>
  </div>
  <div class="stat-box">
    <div class="status {'ok' if n_matspec > 0 else 'bad'}">{status_matspec}</div>
    <div class="value">{n_matspec}</div>
    <div class="label">Material Spec<br>Rows</div>
  </div>
  <div class="stat-box">
    <div class="status {'ok' if n_loads > 0 else 'bad'}">{status_loads}</div>
    <div class="value">{n_loads}</div>
    <div class="label">Load<br>Conditions</div>
  </div>
</div>

<div class="card">
  <h2>&#x2139;&#xFE0F; Report Information</h2>
  <div class="meta-grid">
    <span class="meta-label">Generated:</span><span>{now}</span>
    <span class="meta-label">Drawing File:</span><span>{drawing}</span>
    <span class="meta-label">Project:</span><span>{project}</span>
  </div>
</div>

<div class="card">
  <h2>&#x1F4D0; Design Data</h2>
  {_df_to_html_rows(df_design)}
</div>

<div class="card">
  <h2>&#x1F527; Material Specification</h2>
  {_df_to_html_rows(df_matspec)}
</div>

<div class="card">
  <h2>&#x2696;&#xFE0F; Foundation Loads — Vessel Weights (kgf)</h2>
  {_df_to_html_rows(df_weights)}
</div>

<div class="card">
  <h2>&#x1F4A8; Foundation Loads — Shear &amp; Moment</h2>
  {_df_to_html_rows(df_loads)}
</div>

<footer>Generated by Design Data Pipeline &nbsp;|&nbsp; {now} &nbsp;|&nbsp; {project}</footer>
</body>
</html>"""


# ==============================================================================
# SECTION 10 — MAIN PIPELINE RUNNER
# ==============================================================================

def run_pipeline(file_path: Path, outdir: Path, debug: bool, args) -> None:
    """Execute the full 4-stage extraction pipeline."""
    print("\n" + "=" * 60)
    print("  STAGE 1 / 4 — LOADING DWG")
    print("=" * 60)
    doc = load_document(file_path)

    print("\n" + "=" * 60)
    print("  STAGE 2 / 4 — COLLECTING TEXT ENTITIES")
    print("=" * 60)
    entities = collect_all_entities(doc)

    print("\n" + "=" * 60)
    print("  STAGE 3 / 4 — EXTRACTING TABLES")
    print("=" * 60)
    df_design          = extract_design_data(entities)
    df_matspec         = extract_material_spec(entities)
    df_weights, df_loads = extract_foundation_loads(entities)

    outdir.mkdir(parents=True, exist_ok=True)

    if debug:
        plot_path = outdir / (file_path.stem + "_debug_scatter.png")
        _save_debug_plot(entities, file_path.stem, plot_path)

    print("\n" + "=" * 60)
    print("  STAGE 4 / 4 — EXPORTING RESULTS")
    print("=" * 60)
    export_all(df_design, df_matspec, df_weights, df_loads, outdir)

    html = render_html(df_design, df_matspec, df_weights, df_loads, args)
    report_path = outdir / Path(args.out).name
    report_path.write_text(html, encoding="utf-8")
    print(f"[Export] HTML  -> {report_path}")

    # ── Console summary ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  EXTRACTION SUMMARY")
    print("=" * 60)

    for label, df in [
        ("Design Data",           df_design),
        ("Material Specification", df_matspec),
        ("Foundation Weights",     df_weights),
        ("Foundation Shear & Moment", df_loads),
    ]:
        print(f"\n[{label}]")
        if df.empty:
            print("  (not found or empty)")
        else:
            print(df.to_string(index=False))

    print("\n" + "=" * 60)
    print("  ALL DONE")
    print("=" * 60)
    print(f"  HTML   : {report_path.resolve()}")
    print(f"  XLSX   : {(outdir / 'design_extraction.xlsx').resolve()}")
    print("=" * 60 + "\n")


# ==============================================================================
# SECTION 11 — CLI ENTRY POINT
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="design_pipeline",
        description=(
            "Extract Design Data, Material Specification, and Foundation Load Data\n"
            "from a DWG/DXF engineering drawing — outputs CSV, XLSX, and HTML."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "drawing", metavar="DRAWING",
        help="Path to the .dwg or .dxf file.",
    )
    parser.add_argument(
        "--outdir", "-o", default=None, metavar="DIR",
        help="Directory to write all output files (default: same folder as drawing).",
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUTPUT, metavar="FILE",
        help=f"HTML report filename (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Save a debug scatter-plot PNG of all text entities.",
    )
    parser.add_argument(
        "--project", default=DEFAULT_PROJECT,
        help="Project name shown in the HTML report banner.",
    )

    args = parser.parse_args()

    file_path = Path(args.drawing).resolve()
    if not file_path.exists():
        sys.exit(f"[ERROR] Drawing file not found: {file_path}")

    outdir = Path(args.outdir).resolve() if args.outdir else file_path.parent
    args.drawing = str(file_path)

    try:
        run_pipeline(file_path, outdir, args.debug, args)
    except Exception as exc:
        print(f"\n[ERROR] Pipeline failed: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
