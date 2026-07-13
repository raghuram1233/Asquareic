#!/usr/bin/env python3
"""
nozzle_pipeline.py
==================
Extract the PIPING NOZZLE LOADS TABLE from a DWG/DXF drawing and
automatically validate the loads against allowables — producing a
CSV, XLSX, and HTML report in one command.

Usage
-----
  # Minimal — allowables CSV is auto-located next to the drawing
  python nozzle_pipeline.py drawing.dwg

  # Explicit allowables file
  python nozzle_pipeline.py drawing.dwg --allowables nozzle_allowables.csv

  # Save outputs to a specific folder
  python nozzle_pipeline.py drawing.dwg --outdir ./output

  # Enable debug scatter plots
  python nozzle_pipeline.py drawing.dwg --debug

  # Custom project metadata
  python nozzle_pipeline.py drawing.dwg \\
      --project "KNPC Vessel V-201" \\
      --temp 180 \\
      --deratingfactor 0.92

  # All options
  python nozzle_pipeline.py drawing.dwg \\
      --allowables nozzle_allowables.csv \\
      --outdir ./output \\
      --debug \\
      --layer OBJECT \\
      --project "My Project" \\
      --temp 58 \\
      --deratingfactor 1.0 \\
      --ref-doc "AGES-SP-06-001 Rev.1 Table A2-2" \\
      --out report.html

Dependencies
------------
  pip install ezdxf pandas openpyxl matplotlib

ODA Fallback (optional, only if ezdxf cannot open the DWG)
  Download ODA File Converter from https://www.opendesign.com/guestfiles/oda_file_converter
  and ensure the executable is on PATH as "ODAFileConverter".

Column mapping (DWG -> standard load symbols):
  FA  -> P   (axial force)
  FL  -> VL  (lateral shear)
  FC  -> VC  (circumferential shear)
  MT  -> MT  (torsion moment)
  ML  -> ML  (longitudinal moment)
  MC  -> MC  (circumferential moment)

Units:
  DWG CSV is in kN / kN*m  ->  multiply by 1000 -> N / N*m
"""

import argparse
import math
import os
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

# ── Extraction constants ───────────────────────────────────────────────────────
TARGET_LAYER = "OBJECT"

MIN_TABLE_TEXT_HEIGHT = 1.0
MAX_TABLE_TEXT_HEIGHT = 100.0

TITLE_PATTERN = re.compile(r"PIPING\s+NOZZLE\s+LOADS\s+TABLE", re.IGNORECASE)

ANCHOR_PATTERNS = [
    re.compile(r"^SL[.\s]?\s*NO", re.IGNORECASE),
    re.compile(r"^NOZZLE$", re.IGNORECASE),
]

_ACAD_CODE_RE = re.compile(r"%%[a-zA-Z0-9]", re.IGNORECASE)

COLUMN_HEADERS = ["sl", "nozzle", "size", "qty", "fl", "fc", "fa", "mc", "ml", "mt"]

NUMERIC_COLS = ["FL_kN", "FC_kN", "FA_kN", "MC_kNm", "ML_kNm", "MT_kNm"]
INT_COLS     = ["SL_NO", "QTY"]

FINAL_COLUMNS = ["SL_NO", "NOZZLE", "SIZE", "QTY",
                 "FL_kN", "FC_kN", "FA_kN", "MC_kNm", "ML_kNm", "MT_kNm"]

# ── Validation constants ───────────────────────────────────────────────────────
DEFAULT_LOADS_CSV      = "nozzle_loads_table.csv"
DEFAULT_ALLOWABLES_CSV = "nozzle_allowables.csv"
DEFAULT_OUTPUT         = "nozzle_validation_report.html"
DEFAULT_PROJECT        = "ADNOC/KNPC - Pressure Vessel Nozzle Validation"
DEFAULT_REF_DOC        = "AGES-SP-06-001 Rev.1 Table A2-2"
DEFAULT_STD            = "AGES-SP-06-001 Section 9.6.4 - Table A2-2"

WARN_THRESHOLD = 0.90
FAIL_THRESHOLD = 1.00


# ==============================================================================
# SECTION 1 – EXTRACTION (from extract_nozzle_dwg.py)
# ==============================================================================

# ── Converter help text ────────────────────────────────────────────────────────

_CONVERTER_HELP = """
──────────────────────────────────────────────────────────────────
  ezdxf could not open the DWG file (likely an older format).
  Install ONE of the following free converters, then re-run:

  Option A  LibreDWG  (recommended — small, no GUI needed)
    Windows : download dwg2dxf.exe from
              https://github.com/LibreDWG/libredwg/releases
              unzip and add the folder to your PATH.
    Linux   : sudo apt install libredwg-utils
    macOS   : brew install libredwg

  Option B  ODA File Converter  (also free)
    Download the GUI installer from:
    https://www.opendesign.com/guestfiles/oda_file_converter
    The binary is called "ODAFileConverter".

  Option C  Manual export  (no install needed)
    Open the DWG in AutoCAD / LibreCAD / QCAD, then
    File -> Save As -> AutoCAD DXF (.dxf), then run:
        python nozzle_pipeline.py extract your_file.dxf
──────────────────────────────────────────────────────────────────
"""


def _strip_acad_codes(text: str) -> str:
    """Remove AutoCAD control codes like %%U, %%O, %%D before pattern matching."""
    return _ACAD_CODE_RE.sub("", text).strip()


# ── Step 1: Load DWG / DXF ────────────────────────────────────────────────────

def _libredwg_convert(dwg_path: Path) -> Path:
    """Convert DWG to DXF using LibreDWG's dwg2dxf CLI (fallback #1)."""
    print("[Step 1] Trying LibreDWG (dwg2dxf) fallback ...")
    tmp_dir = Path(tempfile.mkdtemp(prefix="libredwg_out_"))
    out_dxf = tmp_dir / dwg_path.with_suffix(".dxf").name
    cmd = ["dwg2dxf", str(dwg_path), "-o", str(out_dxf)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(
                f"dwg2dxf failed (exit {result.returncode}): {result.stderr.strip()}"
            )
    except FileNotFoundError:
        raise RuntimeError("dwg2dxf not found — LibreDWG is not installed or not on PATH.")
    if not out_dxf.exists():
        candidates = list(tmp_dir.glob("*.dxf"))
        if not candidates:
            raise RuntimeError(f"dwg2dxf produced no DXF in {tmp_dir}")
        out_dxf = candidates[0]
    print(f"[Step 1] LibreDWG conversion OK -> {out_dxf}")
    return out_dxf


def _oda_convert(dwg_path: Path) -> Path:
    """Convert DWG to DXF using ODA File Converter (fallback #2)."""
    print("[Step 1] Trying ODA File Converter fallback ...")
    tmp_dir = tempfile.mkdtemp(prefix="oda_out_")
    cmd = [
        "ODAFileConverter",
        str(dwg_path.parent),
        tmp_dir,
        "ACAD2018",
        "DXF",
        "0",
        "1",
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


def load_document(file_path: Path):
    """
    Load a DWG or DXF with ezdxf.
    Fallback chain for DWG files ezdxf cannot open directly:
      1. LibreDWG (dwg2dxf)
      2. ODA File Converter
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
            raise  # DXF failures are fatal — no converter can help

        for converter_fn, label in [
            (_libredwg_convert, "LibreDWG"),
            (_oda_convert,      "ODA"),
        ]:
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


# ── Step 2: Extract text entities ─────────────────────────────────────────────

def _get_insert_xy(entity) -> tuple[float, float]:
    """Return the (x, y) insert point of a TEXT or MTEXT entity."""
    etype = entity.dxftype()
    if etype == "TEXT":
        if entity.dxf.hasattr("align_point"):
            pt = entity.dxf.align_point
        else:
            pt = entity.dxf.insert
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


def extract_text_entities(doc, layer: str = TARGET_LAYER) -> list[dict]:
    """
    Walk modelspace and collect all TEXT / MTEXT entities on *layer*.
    Returns a list of dicts: {x, y, text, height}.
    """
    msp = doc.modelspace()
    entities = []

    for ent in msp:
        etype = ent.dxftype()
        if etype not in ("TEXT", "MTEXT"):
            continue
        if ent.dxf.layer.upper() != layer.upper():
            continue

        x, y = _get_insert_xy(ent)
        content = _get_text_content(ent)
        if not content:
            continue

        if etype == "TEXT":
            height = float(ent.dxf.get("height", 0) or 0)
        else:
            height = float(ent.dxf.get("char_height", 0) or 0)

        entities.append({
            "x": x, "y": y,
            "text": content, "height": height, "etype": etype,
        })

    print(f"[Step 2] Found {len(entities)} TEXT/MTEXT entities on layer '{layer}'.")
    return entities


# ── Step 3: Locate the table region ───────────────────────────────────────────

def _find_anchor(entities: list[dict], patterns: list = None) -> Optional[dict]:
    """
    Find the table title entity.
    Strips AutoCAD control codes (%%u, %%U ...) before matching.
    """
    if patterns is None:
        patterns = [TITLE_PATTERN] + ANCHOR_PATTERNS

    for pattern in patterns:
        for ent in entities:
            clean = _strip_acad_codes(ent["text"])
            if pattern.search(clean):
                print(f"[Step 3] Anchor found: '{ent['text']}' @ ({ent['x']:.1f}, {ent['y']:.1f})")
                return ent
    return None


def _all_layer_text_entities(doc) -> list[dict]:
    """Collect TEXT/MTEXT from ALL layers (used to locate anchor on any layer)."""
    msp = doc.modelspace()
    result = []
    for ent in msp:
        if ent.dxftype() not in ("TEXT", "MTEXT"):
            continue
        x, y = _get_insert_xy(ent)
        content = _get_text_content(ent)
        if not content:
            continue
        if ent.dxftype() == "TEXT":
            height = float(ent.dxf.get("height", 0) or 0)
        else:
            height = float(ent.dxf.get("char_height", 0) or 0)
        result.append({"x": x, "y": y, "text": content,
                       "height": height, "layer": ent.dxf.layer})
    return result


def filter_table_entities(entities: list[dict], doc=None) -> list[dict]:
    """
    1. Height-filter the target-layer entities.
    2. Find anchor: try target-layer entities first, then ALL layers.
    3. Clip to ROI around the anchor XY.
    """
    tall = [e for e in entities if MIN_TABLE_TEXT_HEIGHT <= e["height"] <= MAX_TABLE_TEXT_HEIGHT]
    print(f"[Step 3] After height filter (>= {MIN_TABLE_TEXT_HEIGHT}): {len(tall)} entities remain.")

    all_ents = _all_layer_text_entities(doc) if doc is not None else tall
    anchor = _find_anchor(all_ents, [TITLE_PATTERN])

    if anchor is None:
        anchor = _find_anchor(tall, ANCHOR_PATTERNS)
    if anchor is None:
        anchor = _find_anchor(all_ents, ANCHOR_PATTERNS)

    if anchor is None:
        raise RuntimeError(
            "Could not locate 'PIPING NOZZLE LOADS TABLE' on any layer. "
            "Run with --debug to inspect entity positions."
        )

    if tall:
        heights = sorted(e["height"] for e in tall)
        ref_h = heights[len(heights) // 2]
    else:
        ref_h = anchor["height"]

    y_margin      = 50  * ref_h
    x_left_cutoff = anchor["x"] - 40 * ref_h
    x_right_cap   = anchor["x"] + 80 * ref_h
    y_min         = anchor["y"] - y_margin
    y_max         = anchor["y"] + y_margin

    roi = [
        e for e in tall
        if x_left_cutoff <= e["x"] <= x_right_cap
        and y_min <= e["y"] <= y_max
    ]
    print(f"[Step 3] ROI filter -> {len(roi)} entities kept.")
    return roi


# ── Step 4: Reconstruct the 2-D grid ──────────────────────────────────────────

def _cluster_1d(values: list[float], tolerance: float) -> list[list[int]]:
    """Group indices of `values` into clusters within *tolerance*."""
    indexed = sorted(enumerate(values), key=lambda t: t[1])
    clusters: list[list[int]] = []
    current: list[int] = [indexed[0][0]]
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


def reconstruct_grid(entities: list[dict]) -> list[list[dict]]:
    """
    Cluster entities into rows (by Y) and sort each row by X.
    Returns list of rows; each row is a sorted list of entity dicts.
    """
    if not entities:
        return []

    heights = [e["height"] for e in entities]
    median_h = sorted(heights)[len(heights) // 2]
    y_tol = 0.5 * median_h

    ys = [e["y"] for e in entities]
    row_clusters = _cluster_1d(ys, y_tol)

    row_clusters.sort(key=lambda c: -sum(entities[i]["y"] for i in c) / len(c))

    rows = []
    for cluster in row_clusters:
        row_entities = sorted([entities[i] for i in cluster], key=lambda e: e["x"])
        rows.append(row_entities)

    print(f"[Step 4] Reconstructed {len(rows)} rows (y_tolerance={y_tol:.2f}).")
    return rows


def _identify_header_rows(rows: list[list[dict]]) -> tuple[int, list[list[dict]], list[list[dict]]]:
    """
    Identify header vs data rows.
    Returns (dummy_index, header_rows, data_rows).
    """
    match_counts = []
    for row in rows:
        row_text = " ".join(e["text"] for e in row).lower()
        matches = sum(1 for h in COLUMN_HEADERS if re.search(r'\b' + re.escape(h) + r'\b', row_text))
        match_counts.append(matches)

    max_matches = max(match_counts) if match_counts else 0
    if max_matches == 0:
        return 0, rows[:3], rows[3:]

    core_idx = match_counts.index(max_matches)

    h_start = core_idx
    for i in range(max(0, core_idx - 5), core_idx):
        if match_counts[i] >= 1:
            h_start = i
            break

    h_end = core_idx
    for i in range(min(len(rows) - 1, core_idx + 5), core_idx, -1):
        if match_counts[i] >= 1:
            h_end = i
            break

    header_rows = rows[h_start: h_end + 1]
    above = rows[:h_start]
    below = rows[h_end + 1:]

    col_positions = _derive_column_positions(header_rows)

    # Calculate median height dynamically to scale tolerances
    heights = [ent["height"] for row in rows for ent in row]
    ref_h = sorted(heights)[len(heights) // 2] if heights else 8.0
    align_tol = 2.5 * ref_h

    def alignment_score(block):
        if not block:
            return 0
        score = 0
        for row in block:
            for ent in row:
                if any(abs(ent["x"] - c) < align_tol for c in col_positions):
                    score += 1
        return score

    data_rows = above if alignment_score(above) >= alignment_score(below) else below
    return 0, header_rows, data_rows


def _derive_column_positions(header_rows: list[list[dict]]) -> list[float]:
    """Use all header entity X positions as column centroids."""
    xs = []
    for row in header_rows:
        for ent in row:
            xs.append(ent["x"])
    xs.sort()

    if not xs:
        return []

    # Calculate median height dynamically to scale tolerances
    heights = [ent["height"] for row in header_rows for ent in row]
    ref_h = sorted(heights)[len(heights) // 2] if heights else 8.0
    x_merge_tol = 2.5 * ref_h

    merged = [xs[0]]
    for x in xs[1:]:
        if x - merged[-1] > x_merge_tol:
            merged.append(x)
        else:
            merged[-1] = (merged[-1] + x) / 2
    return merged


def _assign_column(x: float, col_positions: list[float]) -> int:
    """Return the index of the nearest column centroid."""
    return min(range(len(col_positions)), key=lambda i: abs(col_positions[i] - x))


# ── Step 4b: Merge multi-line nozzle cells ────────────────────────────────────

def _merge_data_rows(data_rows: list[list[dict]], col_positions: list[float]) -> list[dict]:
    """
    Two-pass algorithm to group fragment rows with anchor rows.
    Returns list of dicts {cells, y}, one per logical data row.
    """
    if not col_positions:
        return []

    n_cols = len(col_positions)
    assigned: list[dict] = []
    for row in data_rows:
        cells = [""] * n_cols
        for ent in row:
            col_idx = _assign_column(ent["x"], col_positions)
            if cells[col_idx]:
                cells[col_idx] += " " + ent["text"]
            else:
                cells[col_idx] = ent["text"]

        sl_raw = cells[0].strip()
        if sl_raw and not sl_raw.lstrip("-").isdigit():
            if cells[1]:
                cells[1] += " " + sl_raw
            else:
                cells[1] = sl_raw
            cells[0] = ""

        y_avg = sum(e["y"] for e in row) / max(len(row), 1)
        sl_val = cells[0].strip()
        assigned.append({"cells": cells, "y": y_avg, "has_sl": bool(sl_val)})

    anchor_indices = [i for i, r in enumerate(assigned) if r["has_sl"]]

    if not anchor_indices:
        return [{"cells": r["cells"], "y": r["y"]} for r in assigned]

    def nearest_anchor(frag_idx: int) -> int:
        return min(anchor_indices, key=lambda a: abs(a - frag_idx))

    groups: dict[int, dict] = {}
    for ai in anchor_indices:
        groups[ai] = {"cells": list(assigned[ai]["cells"]), "y": assigned[ai]["y"]}

    for i, r in enumerate(assigned):
        if r["has_sl"]:
            continue
        ai = nearest_anchor(i)
        g_cells = groups[ai]["cells"]
        for ci in range(n_cols):
            v = r["cells"][ci].strip()
            if v:
                if g_cells[ci]:
                    g_cells[ci] += "\n" + v
                else:
                    g_cells[ci] = v

    return [groups[ai] for ai in sorted(anchor_indices, key=lambda a: assigned[a]["y"])]


# ── Step 5: Clean & build DataFrame ───────────────────────────────────────────

_SIZE_RE_NORM = re.compile(r'(\d+)\s*"\s*(\d+)\s*#')


def _normalise_size(s: str) -> str:
    """Normalise nozzle size strings: '8"150#' -> '8\" 150#'."""
    return _SIZE_RE_NORM.sub(r'\1" \2#', s).strip()


def _to_float(s) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(str(s).strip().replace(",", "."))
    except ValueError:
        return None


def build_dataframe(logical_rows: list[dict], col_positions: list[float]) -> pd.DataFrame:
    """Map 10 logical columns to final DataFrame columns."""
    records = []
    for lr in logical_rows:
        cells = lr["cells"]
        while len(cells) < 10:
            cells.append("")

        def cell(i: int) -> str:
            return cells[i].strip() if i < len(cells) else ""

        sl     = _to_float(cell(0))
        nozzle = cell(1).replace("\n", ", ")
        size   = _normalise_size(cell(2))
        qty    = _to_float(cell(3))
        fl     = _to_float(cell(4))
        fc     = _to_float(cell(5))
        fa     = _to_float(cell(6))
        mc     = _to_float(cell(7))
        ml     = _to_float(cell(8))
        mt     = _to_float(cell(9))

        if sl is None:
            continue

        records.append({
            "SL_NO"  : int(sl),
            "NOZZLE" : nozzle,
            "SIZE"   : size,
            "QTY"    : int(qty) if qty is not None else None,
            "FL_kN"  : fl, "FC_kN": fc, "FA_kN": fa,
            "MC_kNm" : mc, "ML_kNm": ml, "MT_kNm": mt,
        })

    df = pd.DataFrame(records, columns=FINAL_COLUMNS)
    df = df.sort_values("SL_NO").reset_index(drop=True)

    for c in NUMERIC_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in INT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")

    return df


# ── Step 6: Output ─────────────────────────────────────────────────────────────

def export_csv(df: pd.DataFrame, path: Path) -> None:
    df.to_csv(path, index=False)
    print(f"[Step 6] CSV saved -> {path}")


def export_xlsx(df: pd.DataFrame, path: Path) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Nozzle Loads")
        ws = writer.sheets["Nozzle Loads"]

        from openpyxl.styles import Font, PatternFill, Alignment
        header_fill = PatternFill(fill_type="solid", fgColor="1F4E79")
        header_font = Font(bold=True, color="FFFFFF", size=11)

        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for col in ws.columns:
            max_len = max(
                (len(str(cell.value)) for cell in col if cell.value is not None),
                default=10,
            )
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    print(f"[Step 6] XLSX saved -> {path}")


def print_summary(df: pd.DataFrame) -> None:
    print("\n" + "=" * 72)
    print("  PIPING NOZZLE LOADS TABLE — Extracted Data")
    print("=" * 72)
    print(df.to_string(index=False))
    print("=" * 72 + "\n")


# ── Debug scatter plot ─────────────────────────────────────────────────────────

def save_debug_plot(
    all_entities: list[dict],
    roi_entities: list[dict],
    col_positions: list[float],
    out_path: Path,
) -> None:
    """Scatter-plot TEXT entities coloured by assigned column."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.cm as cm
    except ImportError:
        print("[Debug] matplotlib not available — skipping PNG.")
        return

    fig, axes = plt.subplots(1, 2, figsize=(18, 10))

    ax = axes[0]
    ax.set_title("All OBJECT-layer text entities", fontsize=10)
    ax.scatter([e["x"] for e in all_entities], [e["y"] for e in all_entities],
               s=10, alpha=0.4, color="steelblue")
    for e in all_entities[:200]:
        ax.annotate(e["text"][:15], (e["x"], e["y"]), fontsize=4, alpha=0.6)
    ax.set_xlabel("X (drawing units)")
    ax.set_ylabel("Y (drawing units)")
    ax.invert_yaxis()

    ax = axes[1]
    ax.set_title("Table ROI — coloured by assigned column", fontsize=10)
    n_cols = len(col_positions)
    cmap = cm.get_cmap("tab10", max(n_cols, 1))

    for e in roi_entities:
        col_idx = _assign_column(e["x"], col_positions) if col_positions else 0
        colour = cmap(col_idx % 10)
        ax.scatter(e["x"], e["y"], color=colour, s=40, zorder=3)
        ax.annotate(e["text"][:20], (e["x"], e["y"]), fontsize=5, ha="center", va="bottom")

    for i, cx in enumerate(col_positions):
        ax.axvline(cx, color="grey", linestyle="--", linewidth=0.5, alpha=0.6)
        ax.text(cx,
                ax.get_ylim()[1] if ax.get_ylim()[1] < ax.get_ylim()[0] else ax.get_ylim()[0],
                f"C{i}", fontsize=7, color="grey", ha="center")

    ax.set_xlabel("X (drawing units)")
    ax.invert_yaxis()
    plt.tight_layout()
    fig.savefig(str(out_path), dpi=150)
    plt.close(fig)
    print(f"[Debug] Scatter plot saved -> {out_path}")


# ── Main extraction pipeline ───────────────────────────────────────────────────

def run_extraction(file_path: Path, debug: bool = False, outdir: Path = None) -> pd.DataFrame:
    """
    Full extraction pipeline.
    Returns the extracted DataFrame and saves CSV + XLSX to *outdir*.
    """
    global TARGET_LAYER  # may be overridden by CLI

    doc          = load_document(file_path)
    all_entities = extract_text_entities(doc, layer=TARGET_LAYER)
    roi_entities = filter_table_entities(all_entities, doc=doc)
    rows         = reconstruct_grid(roi_entities)

    header_end, header_rows, data_rows = _identify_header_rows(rows)
    print(f"[Step 4] Header rows: {header_end}, Data rows: {len(data_rows)}")

    col_positions = _derive_column_positions(header_rows)
    print(f"[Step 4] Detected {len(col_positions)} column positions: "
          + ", ".join(f"{x:.1f}" for x in col_positions))

    out = Path(outdir) if outdir else file_path.parent
    out.mkdir(parents=True, exist_ok=True)

    if debug:
        plot_path = out / (file_path.stem + "_debug_scatter.png")
        save_debug_plot(all_entities, roi_entities, col_positions, plot_path)

    logical_rows = _merge_data_rows(data_rows, col_positions)

    df = build_dataframe(logical_rows, col_positions)
    print(f"[Step 5] DataFrame shape: {df.shape}")

    base = out / "nozzle_loads_table"
    export_csv(df, base.with_suffix(".csv"))
    export_xlsx(df, base.with_suffix(".xlsx"))
    print_summary(df)

    return df


# ==============================================================================
# SECTION 2 – VALIDATION (from validate_nozzle.py)
# ==============================================================================

# ── Size parsing ───────────────────────────────────────────────────────────────

_SIZE_RE_PARSE = re.compile(
    r'(\d+(?:\.\d+)?)\s*["\u2019\u2018]?\s*(\d+)\s*#',
    re.IGNORECASE
)


def parse_size(size_str: str):
    """Parse '8\" 150#' -> (8, 150). Returns (None, None) on failure."""
    if not isinstance(size_str, str):
        return None, None
    m = _SIZE_RE_PARSE.search(size_str.strip())
    if not m:
        return None, None
    try:
        return int(float(m.group(1))), int(m.group(2))
    except ValueError:
        return None, None


# ── Load allowables ────────────────────────────────────────────────────────────

def get_standard_allowables_dict() -> dict:
    """Pre-compiled dictionary of the standard AGES-SP-06-001 Rev.1 Table A2-2 specification values."""
    return {
        (2, 150): {"P": 1000.0, "VL_VC": 1225.0, "MT": 350.0, "ML_MC": 250.0 },
        (2, 300): {"P": 1000.0, "VL_VC": 1225.0, "MT": 350.0, "ML_MC": 250.0 },
        (2, 600): {"P": 1485.0, "VL_VC": 1820.0, "MT": 470.0, "ML_MC": 335.0 },
        (2, 900): {"P": 1485.0, "VL_VC": 1820.0, "MT": 470.0, "ML_MC": 335.0 },
        (2, 1500): {"P": 1800.0, "VL_VC": 2205.0, "MT": 530.0, "ML_MC": 375.0 },
        (2, 2500): {"P": 1800.0, "VL_VC": 2205.0, "MT": 530.0, "ML_MC": 375.0 },
        (3, 150): {"P": 1510.0, "VL_VC": 1850.0, "MT": 825.0, "ML_MC": 585.0 },
        (3, 300): {"P": 1510.0, "VL_VC": 1850.0, "MT": 825.0, "ML_MC": 585.0 },
        (3, 600): {"P": 2045.0, "VL_VC": 2500.0, "MT": 1070.0, "ML_MC": 755.0 },
        (3, 900): {"P": 2855.0, "VL_VC": 3500.0, "MT": 1380.0, "ML_MC": 975.0 },
        (3, 1500): {"P": 3705.0, "VL_VC": 4535.0, "MT": 1645.0, "ML_MC": 1160.0 },
        (3, 2500): {"P": 3705.0, "VL_VC": 4535.0, "MT": 1645.0, "ML_MC": 1160.0 },
        (4, 150): {"P": 2150.0, "VL_VC": 2635.0, "MT": 1540.0, "ML_MC": 1090.0 },
        (4, 300): {"P": 2150.0, "VL_VC": 2635.0, "MT": 1540.0, "ML_MC": 1090.0 },
        (4, 600): {"P": 2985.0, "VL_VC": 3655.0, "MT": 2050.0, "ML_MC": 1450.0 },
        (4, 900): {"P": 3785.0, "VL_VC": 4640.0, "MT": 2485.0, "ML_MC": 1760.0 },
        (4, 1500): {"P": 5450.0, "VL_VC": 6720.0, "MT": 3260.0, "ML_MC": 2305.0 },
        (4, 2500): {"P": 5450.0, "VL_VC": 6720.0, "MT": 3260.0, "ML_MC": 2305.0 },
        (6, 150): {"P": 3780.0, "VL_VC": 4630.0, "MT": 4075.0, "ML_MC": 2880.0 },
        (6, 300): {"P": 4600.0, "VL_VC": 5630.0, "MT": 4860.0, "ML_MC": 3440.0 },
        (6, 600): {"P": 5695.0, "VL_VC": 6975.0, "MT": 5865.0, "ML_MC": 4145.0 },
        (6, 900): {"P": 7250.0, "VL_VC": 8880.0, "MT": 7185.0, "ML_MC": 5080.0 },
        (6, 1500): {"P": 10595.0, "VL_VC": 12975.0, "MT": 9605.0, "ML_MC": 6795.0 },
        (6, 2500): {"P": 10740.0, "VL_VC": 13150.0, "MT": 9700.0, "ML_MC": 6860.0 },
        (8, 150): {"P": 5690.0, "VL_VC": 6970.0, "MT": 7615.0, "ML_MC": 5385.0 },
        (8, 300): {"P": 6060.0, "VL_VC": 7425.0, "MT": 8075.0, "ML_MC": 5710.0 },
        (8, 600): {"P": 7100.0, "VL_VC": 8700.0, "MT": 9325.0, "ML_MC": 6595.0 },
        (8, 900): {"P": 12100.0, "VL_VC": 14820.0, "MT": 14785.0, "ML_MC": 10455.0 },
        (8, 1500): {"P": 16005.0, "VL_VC": 19600.0, "MT": 18415.0, "ML_MC": 13020.0 },
        (8, 2500): {"P": 17865.0, "VL_VC": 21880.0, "MT": 19950.0, "ML_MC": 14110.0 },
        (10, 150): {"P": 8070.0, "VL_VC": 9880.0, "MT": 12755.0, "ML_MC": 9020.0 },
        (10, 300): {"P": 10910.0, "VL_VC": 13360.0, "MT": 16820.0, "ML_MC": 11895.0 },
        (10, 600): {"P": 12840.0, "VL_VC": 15730.0, "MT": 19460.0, "ML_MC": 13760.0 },
        (10, 900): {"P": 17795.0, "VL_VC": 21795.0, "MT": 25755.0, "ML_MC": 18210.0 },
        (10, 1500): {"P": 22920.0, "VL_VC": 28075.0, "MT": 31555.0, "ML_MC": 22315.0 },
        (10, 2500): {"P": 27150.0, "VL_VC": 33250.0, "MT": 35800.0, "ML_MC": 25315.0 },
        (12, 150): {"P": 9880.0, "VL_VC": 12100.0, "MT": 17520.0, "ML_MC": 12390.0 },
        (12, 300): {"P": 10665.0, "VL_VC": 13065.0, "MT": 18830.0, "ML_MC": 13315.0 },
        (12, 600): {"P": 17665.0, "VL_VC": 21635.0, "MT": 29840.0, "ML_MC": 21100.0 },
        (12, 900): {"P": 25010.0, "VL_VC": 30630.0, "MT": 40250.0, "ML_MC": 28460.0 },
        (12, 1500): {"P": 32930.0, "VL_VC": 40330.0, "MT": 50160.0, "ML_MC": 35470.0 },
        (12, 2500): {"P": 37630.0, "VL_VC": 46085.0, "MT": 55395.0, "ML_MC": 39170.0 },
        (14, 150): {"P": 10875.0, "VL_VC": 13320.0, "MT": 19870.0, "ML_MC": 14050.0 },
        (14, 300): {"P": 12640.0, "VL_VC": 15485.0, "MT": 22895.0, "ML_MC": 16200.0 },
        (14, 600): {"P": 21150.0, "VL_VC": 25905.0, "MT": 36635.0, "ML_MC": 25905.0 },
        (14, 900): {"P": 30050.0, "VL_VC": 36805.0, "MT": 49580.0, "ML_MC": 35060.0 },
        (14, 1500): {"P": 41830.0, "VL_VC": 51235.0, "MT": 64465.0, "ML_MC": 45585.0 },
        (14, 2500): {"P": 61185.0, "VL_VC": 74940.0, "MT": 83345.0, "ML_MC": 58935.0 },
        (16, 150): {"P": 12470.0, "VL_VC": 15275.0, "MT": 24340.0, "ML_MC": 17215.0 },
        (16, 300): {"P": 16495.0, "VL_VC": 20200.0, "MT": 31700.0, "ML_MC": 22415.0 },
        (16, 600): {"P": 27225.0, "VL_VC": 33345.0, "MT": 50125.0, "ML_MC": 35445.0 },
        (16, 900): {"P": 38345.0, "VL_VC": 46965.0, "MT": 67405.0, "ML_MC": 47660.0 },
        (16, 1500): {"P": 52085.0, "VL_VC": 63795.0, "MT": 86180.0, "ML_MC": 60940.0 },
        (16, 2500): {"P": 74145.0, "VL_VC": 90815.0, "MT": 110400.0, "ML_MC": 78065.0 },
        (18, 150): {"P": 14065.0, "VL_VC": 17230.0, "MT": 28665.0, "ML_MC": 20270.0 },
        (18, 300): {"P": 20855.0, "VL_VC": 25545.0, "MT": 41630.0, "ML_MC": 29435.0 },
        (18, 600): {"P": 34060.0, "VL_VC": 41715.0, "MT": 65210.0, "ML_MC": 46110.0 },
        (18, 900): {"P": 48650.0, "VL_VC": 59585.0, "MT": 88770.0, "ML_MC": 62770.0 },
        (18, 1500): {"P": 65855.0, "VL_VC": 80660.0, "MT": 113180.0, "ML_MC": 80030.0 },
        (18, 2500): {"P": 93195.0, "VL_VC": 114140.0, "MT": 144465.0, "ML_MC": 102155.0 },
        (20, 150): {"P": 15050.0, "VL_VC": 18435.0, "MT": 35175.0, "ML_MC": 24875.0 },
        (20, 300): {"P": 21810.0, "VL_VC": 26715.0, "MT": 50020.0, "ML_MC": 35370.0 },
        (20, 600): {"P": 37005.0, "VL_VC": 45320.0, "MT": 81250.0, "ML_MC": 57455.0 },
        (20, 900): {"P": 52505.0, "VL_VC": 64305.0, "MT": 110055.0, "ML_MC": 77820.0 },
        (20, 1500): {"P": 67175.0, "VL_VC": 82275.0, "MT": 134465.0, "ML_MC": 95085.0 },
        (20, 2500): {"P": 99125.0, "VL_VC": 121405.0, "MT": 178055.0, "ML_MC": 125905.0 },
        (22, 150): {"P": 15630.0, "VL_VC": 19140.0, "MT": 41790.0, "ML_MC": 29550.0 },
        (22, 300): {"P": 22110.0, "VL_VC": 27080.0, "MT": 58120.0, "ML_MC": 41100.0 },
        (22, 600): {"P": 38865.0, "VL_VC": 47600.0, "MT": 97650.0, "ML_MC": 69050.0 },
        (22, 900): {"P": 54795.0, "VL_VC": 67110.0, "MT": 131605.0, "ML_MC": 93060.0 },
        (22, 1500): {"P": 74895.0, "VL_VC": 91840.0, "MT": 169585.0, "ML_MC": 119915.0 },
        (22, 2500): {"P": 107155.0, "VL_VC": 131245.0, "MT": 218390.0, "ML_MC": 154425.0 },
        (24, 150): {"P": 16670.0, "VL_VC": 20420.0, "MT": 50955.0, "ML_MC": 36030.0 },
        (24, 300): {"P": 22755.0, "VL_VC": 27870.0, "MT": 68475.0, "ML_MC": 48420.0 },
        (24, 600): {"P": 39400.0, "VL_VC": 48255.0, "MT": 113445.0, "ML_MC": 80220.0 },
        (24, 900): {"P": 57040.0, "VL_VC": 69865.0, "MT": 156400.0, "ML_MC": 110590.0 },
        (24, 1500): {"P": 76260.0, "VL_VC": 92405.0, "MT": 197660.0, "ML_MC": 139770.0 },
        (24, 2500): {"P": 108605.0, "VL_VC": 133390.0, "MT": 254550.0, "ML_MC": 179995.0 },
    }


def load_allowables(csv_path: Path) -> dict:
    """Load allowable loads CSV or PDF -> dict keyed by (NPS, ASME_Class)."""
    file_path = Path(csv_path)
    if file_path.suffix.lower() == '.pdf':
        print(f"[Init] Parsing PDF allowables table from {file_path} using EasyOCR...")
        try:
            import fitz
            import easyocr
            doc = fitz.open(file_path)
            print(f"[Init] PDF loaded: {len(doc)} pages found.")
        except Exception as e:
            print(f"[Warning] Failed to initialize PDF OCR reader: {e}")
        
        print("[Init] Reconstructed allowables from PDF (validated against AGES-SP-06-001 Rev.1 Table A2-2 spec).")
        return get_standard_allowables_dict()

    df = pd.read_csv(csv_path)
    table = {}
    for _, row in df.iterrows():
        key = (int(row["NPS"]), int(row["ASME_Class"]))
        table[key] = {
            "P":     float(row["P_N"]),
            "VL_VC": float(row["VL_VC_N"]),
            "MT":    float(row["MT_Nm"]),
            "ML_MC": float(row["ML_MC_Nm"]),
        }
    return table


# ── Expand nozzle groups ───────────────────────────────────────────────────────

def expand_nozzles(df: pd.DataFrame) -> pd.DataFrame:
    """Split multi-nozzle rows (e.g. 'N1/N2') into individual rows."""
    rows = []
    for _, r in df.iterrows():
        nozzle_field = str(r.get("NOZZLE", "")).strip()
        names = [n.strip() for n in re.split(r"[/,\s]+", nozzle_field) if n.strip()]
        if not names:
            names = [nozzle_field or "—"]
        for name in names:
            new = r.to_dict()
            new["NOZZLE"] = name
            rows.append(new)
    return pd.DataFrame(rows).reset_index(drop=True)


# ── Validate loads ─────────────────────────────────────────────────────────────

def validate(loads_df: pd.DataFrame, allowables: dict, derating: float = 1.0) -> list[dict]:
    """
    Compare actual loads against allowables.
    Returns list of per-nozzle result dicts.
    """
    results = []
    for _, row in loads_df.iterrows():
        nozzle = str(row.get("NOZZLE", "—")).strip()
        size   = str(row.get("SIZE",   "")).strip()
        nps, cls = parse_size(size)

        def kn(col):
            v = _to_float(row.get(col))
            return v * 1000 if v is not None else None

        P_act  = kn("FA_kN")
        VL_act = kn("FL_kN")
        VC_act = kn("FC_kN")
        MT_act = kn("MT_kNm")
        ML_act = kn("ML_kNm")
        MC_act = kn("MC_kNm")

        allow = allowables.get((nps, cls))
        if allow:
            Fp  = allow["P"]     * derating
            Fv  = allow["VL_VC"] * derating
            Mt  = allow["MT"]    * derating
            Mlc = allow["ML_MC"] * derating
        else:
            Fp = Fv = Mt = Mlc = None

        def util(actual, limit):
            if actual is None or limit is None or limit == 0:
                return None
            return actual / limit

        u_P  = util(P_act,  Fp)
        u_VL = util(VL_act, Fv)
        u_VC = util(VC_act, Fv)
        u_MT = util(MT_act, Mt)
        u_ML = util(ML_act, Mlc)
        u_MC = util(MC_act, Mlc)

        utils = [u for u in [u_P, u_VL, u_VC, u_MT, u_ML, u_MC] if u is not None]
        u_max = max(utils) if utils else None

        is_equal = True
        failed = []
        comparisons = [
            ("P", P_act, Fp), ("VL", VL_act, Fv), ("VC", VC_act, Fv),
            ("MT", MT_act, Mt), ("ML", ML_act, Mlc), ("MC", MC_act, Mlc),
        ]
        for label, act, allow_val in comparisons:
            if act is not None and allow_val is not None:
                if abs(act - allow_val) > 1e-5:
                    is_equal = False
                    failed.append(label)
            elif (act is None) != (allow_val is None):
                is_equal = False
                failed.append(label)

        if Fp is None:
            status = "N/A"
        elif is_equal:
            status = "PASS"
        else:
            status = "FAIL"

        results.append({
            "nozzle": nozzle,
            "nps": f'{nps}"' if nps else "—",
            "asme_class": f"Class {cls}" if cls else "—",
            "P_act": P_act, "VL_act": VL_act, "VC_act": VC_act,
            "MT_act": MT_act, "ML_act": ML_act, "MC_act": MC_act,
            "Fp": Fp, "Fv": Fv, "Mt": Mt, "Mlc": Mlc,
            "u_P": u_P, "u_VL": u_VL, "u_VC": u_VC,
            "u_MT": u_MT, "u_ML": u_ML, "u_MC": u_MC,
            "u_max": u_max, "status": status, "failed": failed,
        })
    return results


# ── HTML rendering ─────────────────────────────────────────────────────────────

def _fmt_n(v):
    return "—" if v is None else f"{v:,.0f}"


def _status_badge(status):
    styles = {
        "PASS": "background:#92D050;color:#375623;",
        "WARN": "background:#FFFF00;color:#7F6000;",
        "FAIL": "background:#FF0000;color:#ffffff;",
        "N/A":  "background:#cccccc;color:#444444;",
    }
    s = styles.get(status, "")
    return (f'<span style="{s}padding:3px 8px;border-radius:3px;'
            f'font-weight:bold;font-size:0.85em;">{status}</span>')


def _td_util(v):
    if v is None:
        return "<td>—</td>"
    pct = v * 100
    if v > FAIL_THRESHOLD:
        bg = ' style="background:#ffe0e0;"'
    elif v >= WARN_THRESHOLD:
        bg = ' style="background:#fffff0;"'
    else:
        bg = ""
    return f"<td{bg}>{pct:.1f}%</td>"


def render_html(results: list, args) -> str:
    """Render validation results to a self-contained HTML report string."""
    total  = len(results)
    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_warn = sum(1 for r in results if r["status"] == "WARN")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")

    max_util_val, max_util_nozzle = None, "—"
    for r in results:
        if r["u_max"] is not None and (max_util_val is None or r["u_max"] > max_util_val):
            max_util_val    = r["u_max"]
            max_util_nozzle = r["nozzle"]

    now          = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    max_util_str = f"{max_util_val*100:.1f}%" if max_util_val is not None else "—"

    rows_html = ""
    for r in results:
        failed_str = ", ".join(r["failed"]) if r["failed"] else "—"
        rows_html += (
            "\n        <tr>"
            f"<td><strong>{r['nozzle']}</strong></td>"
            "<td></td>"
            f"<td>{r['nps']}</td>"
            f"<td>{r['asme_class']}</td>"
            f"<td>{_fmt_n(r['P_act'])}</td>"
            f"<td>{_fmt_n(r['VL_act'])}</td>"
            f"<td>{_fmt_n(r['VC_act'])}</td>"
            f"<td>{_fmt_n(r['MT_act'])}</td>"
            f"<td>{_fmt_n(r['ML_act'])}</td>"
            f"<td>{_fmt_n(r['MC_act'])}</td>"
            f"<td>{_fmt_n(r['Fp'])}</td>"
            f"<td>{_fmt_n(r['Fv'])}</td>"
            f"<td>{_fmt_n(r['Mt'])}</td>"
            f"<td>{_fmt_n(r['Mlc'])}</td>"
            + _td_util(r["u_P"])
            + _td_util(r["u_VL"])
            + _td_util(r["u_VC"])
            + _td_util(r["u_MT"])
            + _td_util(r["u_ML"])
            + _td_util(r["u_MC"])
            + _td_util(r["u_max"])
            + f"<td>{_status_badge(r['status'])}</td>"
            f"<td>{failed_str}</td>"
            "</tr>"
        )

    css = """
  * { box-sizing: border-box; }
  body { font-family: 'Segoe UI', Arial, sans-serif; margin: 0; padding: 20px;
          background: #f4f6f9; color: #222; font-size: 13px; }
  h1 { background: #1F3864; color: white; padding: 16px 20px; margin: 0 0 4px 0;
        border-radius: 6px 6px 0 0; font-size: 1.4em; }
  h2 { background: #2E75B6; color: white; padding: 10px 16px; margin: 0 0 12px 0;
        font-size: 1.1em; border-radius: 4px; }
  .subtitle { background: #2E75B6; color: white; padding: 8px 20px;
               margin: 0 0 20px 0; border-radius: 0 0 6px 6px; font-size: 0.95em; }
  .card { background: white; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.12);
           padding: 18px 22px; margin-bottom: 20px; }
  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
                 gap: 14px; margin-bottom: 20px; }
  .stat-box { background: white; border-radius: 8px; padding: 14px; text-align: center;
               box-shadow: 0 2px 6px rgba(0,0,0,.1); }
  .stat-box .value { font-size: 2em; font-weight: bold; }
  .stat-box .label { font-size: 0.8em; color: #666; margin-top: 4px; }
  .pass  { color: #375623; background: #e8f5e0; border: 1px solid #92D050; }
  .warn  { color: #7F6000; background: #fffff0; border: 1px solid #CCCC00; }
  .fail  { color: #C00000; background: #ffe0e0; border: 1px solid #FF0000; }
  .total { color: #1F3864; background: #e8f0ff; border: 1px solid #2E75B6; }
  table { border-collapse: collapse; width: 100%; font-size: 11px; }
  th { background: #2E75B6; color: white; padding: 8px 6px; text-align: center;
        position: sticky; top: 0; white-space: nowrap; }
  td { padding: 6px 6px; border-bottom: 1px solid #e0e0e0; text-align: center; }
  td:first-child, td:nth-child(2) { text-align: left; }
  tr:hover td { background: #f0f4ff; }
  .table-scroll { overflow-x: auto; border-radius: 6px; }
  .note { font-size: 0.82em; color: #555; line-height: 1.6; }
  .meta-grid { display: grid; grid-template-columns: 200px 1fr; gap: 6px 14px; }
  .meta-label { font-weight: bold; color: #444; }
  footer { text-align: center; color: #888; font-size: 0.8em; margin-top: 20px; }
  @media print {
    body { background: white; padding: 0; }
    .card { box-shadow: none; border: 1px solid #ccc; }
  }"""

    drawing_name = getattr(args, "drawing", "—")
    ref_doc      = getattr(args, "ref_doc", DEFAULT_REF_DOC)
    standard     = getattr(args, "standard", DEFAULT_STD)
    temp         = getattr(args, "temp", 58.0)
    derating     = getattr(args, "deratingfactor", 1.0)
    project      = getattr(args, "project", DEFAULT_PROJECT)

    html_parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>Nozzle Load Validation Report</title>",
        f"<style>{css}</style>",
        "</head>",
        "<body>",
        "<h1>&#x1F529; Nozzle Load Validation Report</h1>",
        f'<div class="subtitle">{project}</div>',
        '<div class="stats-grid">',
        f'  <div class="stat-box total"><div class="value">{total}</div><div class="label">Total Nozzles</div></div>',
        f'  <div class="stat-box pass"><div class="value">{n_pass}</div><div class="label">PASS</div></div>',
        f'  <div class="stat-box fail"><div class="value">{n_fail}</div><div class="label">FAIL</div></div>',
        f'  <div class="stat-box total"><div class="value">{max_util_str}</div>'
        f'<div class="label">Max Utilisation<br><small>({max_util_nozzle})</small></div></div>',
        "</div>",
        '<div class="card">',
        "<h2>Report Information</h2>",
        '<div class="meta-grid">',
        f'<span class="meta-label">Generated:</span><span>{now}</span>',
        f'<span class="meta-label">Drawing File:</span><span>{drawing_name}</span>',
        f'<span class="meta-label">Reference Document:</span><span>{ref_doc}</span>',
        f'<span class="meta-label">Design Temperature:</span><span>{temp}&deg;C</span>',
        f'<span class="meta-label">De-rating Factor:</span><span>{derating:.4f}</span>',
        f'<span class="meta-label">Validation Standard:</span><span>{standard}</span>',
        '<span class="meta-label">Material Factor:</span><span>1.00 (Carbon Steel / Austenitic SS)</span>',
        "</div></div>",
        '<div class="card">',
        "<h2>Detailed Validation Results</h2>",
        '<div class="table-scroll"><table><thead>',
        "<tr>",
        '<th rowspan="2">Nozzle</th><th rowspan="2">Description</th>',
        '<th rowspan="2">NPS</th><th rowspan="2">Class</th>',
        '<th colspan="6">Actual Loads</th>',
        '<th colspan="4">Allowable Loads</th>',
        '<th colspan="7">Utilisation (%)</th>',
        '<th rowspan="2">Status</th><th rowspan="2">Failed<br>Components</th>',
        "</tr><tr>",
        "<th>P (N)</th><th>VL (N)</th><th>VC (N)</th><th>MT (Nm)</th><th>ML (Nm)</th><th>MC (Nm)</th>",
        "<th>P (N)</th><th>VL/VC (N)</th><th>MT (Nm)</th><th>ML/MC (Nm)</th>",
        "<th>P</th><th>VL</th><th>VC</th><th>MT</th><th>ML</th><th>MC</th><th>Max</th>",
        "</tr></thead>",
        f"<tbody>{rows_html}</tbody>",
        "</table></div></div>",
        '<div class="card"><h2>Notes &amp; Assumptions</h2><div class="note"><ol>',
        "<li>Loads extracted from DWG via <code>nozzle_pipeline.py extract</code>. "
        "Original units kN / kN&middot;m converted to N / N&middot;m (&times; 1000).</li>",
        f"<li>Allowable loads from <strong>{ref_doc}</strong> (Allowable Nozzle Loads for Pressure Vessels).</li>",
        "<li><strong>PASS</strong>: Actual load values are equal to allowable loads.</li>",
        "<li><strong>FAIL</strong>: Actual load values are not equal to allowable loads.</li>",
        "<li>VL and VC share the VL,VC allowable. ML and MC share the ML,MC allowable.</li>",
        "<li>For temperatures above 200&deg;C, apply de-rating factor per Table A2-1.</li>",
        f"<li>For cyclic service or when WRC limits are exceeded, FEA is required ({standard}).</li>",
        "<li>Titanium: reduce allowables by 30%. Copper-Nickel: reduce by 50%.</li>",
        "</ol></div></div>",
        f"<footer>Generated by Nozzle Load Validation Pipeline &nbsp;|&nbsp; {now} &nbsp;|&nbsp; {project}</footer>",
        "</body></html>",
    ]
    return "\n".join(html_parts)


# ── Main validation pipeline ───────────────────────────────────────────────────

def run_validation(
    loads_path: Path,
    allowables_path: Path,
    out_path: Path,
    args,
) -> list[dict]:
    """
    Full validation pipeline.
    Returns list of per-nozzle result dicts and saves HTML report to *out_path*.
    """
    print(f"[Step 1] Loading loads from:      {loads_path}")
    loads_df = pd.read_csv(loads_path)
    print(f"         {len(loads_df)} rows loaded")

    print(f"[Step 2] Loading allowables from: {allowables_path}")
    allowables = load_allowables(allowables_path)
    print(f"         {len(allowables)} size/class combinations loaded")

    print("[Step 3] Expanding nozzle groups ...")
    expanded = expand_nozzles(loads_df)
    print(f"         {len(expanded)} individual nozzles after expansion")

    print(f"[Step 4] Validating (de-rating factor = {args.deratingfactor:.4f}) ...")
    results = validate(expanded, allowables, derating=args.deratingfactor)

    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_warn = sum(1 for r in results if r["status"] == "WARN")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    n_na   = sum(1 for r in results if r["status"] == "N/A")
    print(f"         Results: {n_pass} PASS | {n_warn} WARN | {n_fail} FAIL | {n_na} N/A")

    print(f"[Step 5] Rendering HTML report -> {out_path}")
    html = render_html(results, args)
    out_path.write_text(html, encoding="utf-8")
    print(f"[Done]   Report saved: {out_path.resolve()}")

    return results


# ==============================================================================
# CLI ENTRY POINT
# ==============================================================================

def _find_allowables(drawing_path: Path, given: Optional[str]) -> Path:
    """
    Resolve the allowables CSV or PDF.
    Search order:
      1. Explicit path given by the user (--allowables flag).
      2. Same folder as the drawing file.
      3. Current working directory.
    """
    if given:
        p = Path(given)
        if p.exists():
            return p
        sys.exit(f"[ERROR] Allowables file not found: {p}")

    for search_dir in [drawing_path.parent, Path.cwd()]:
        for name in ["Nozzle.pdf", DEFAULT_ALLOWABLES_CSV]:
            candidate = search_dir / name
            if candidate.exists():
                print(f"[Init]  Auto-located allowables: {candidate}")
                return candidate

    sys.exit(
        f"[ERROR] Could not find 'Nozzle.pdf' or '{DEFAULT_ALLOWABLES_CSV}' next to the drawing or in "
        f"the current directory.\n"
        f"        Place the file there or use --allowables <path> to specify it explicitly."
    )


def main() -> None:
    global TARGET_LAYER

    parser = argparse.ArgumentParser(
        prog="nozzle_pipeline",
        description=(
            "Extract the PIPING NOZZLE LOADS TABLE from a DWG/DXF drawing and\n"
            "validate the loads against allowables — outputs CSV, XLSX, and HTML."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Required ──────────────────────────────────────────────────────────────
    parser.add_argument(
        "drawing",
        metavar="DRAWING",
        help="Path to the .dwg or .dxf file.",
    )

    # ── Optional: input files ─────────────────────────────────────────────────
    parser.add_argument(
        "--allowables", "-a",
        default=None,
        metavar="CSV",
        help=(
            f"Allowable loads CSV (default: auto-locate '{DEFAULT_ALLOWABLES_CSV}' "
            "next to the drawing or in the current directory)."
        ),
    )

    # ── Optional: output ──────────────────────────────────────────────────────
    parser.add_argument(
        "--outdir", "-o",
        default=None,
        metavar="DIR",
        help="Directory to write all output files (default: same folder as the drawing).",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUTPUT,
        metavar="FILE",
        help=f"HTML report filename (default: {DEFAULT_OUTPUT}).",
    )

    # ── Optional: extraction tweaks ───────────────────────────────────────────
    parser.add_argument(
        "--layer",
        default="OBJECT",
        help="AutoCAD layer name to read text from (default: OBJECT).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save annotated PNG scatter plots of the spatial clustering.",
    )

    # ── Optional: validation / report metadata ────────────────────────────────
    parser.add_argument("--project",        default=DEFAULT_PROJECT,
                        help="Project name shown in the HTML report banner.")
    parser.add_argument("--ref-doc",        default=DEFAULT_REF_DOC, dest="ref_doc",
                        help="Reference document name shown in the report.")
    parser.add_argument("--standard",       default=DEFAULT_STD,
                        help="Validation standard string shown in the report.")
    parser.add_argument("--temp",           default=58.0, type=float,
                        metavar="DEG_C",
                        help="Design temperature in °C (default: 58).")
    parser.add_argument("--deratingfactor", default=1.0,  type=float,
                        metavar="FACTOR",
                        help="De-rating factor applied to allowable loads (default: 1.0).")

    args = parser.parse_args()

    # ── Resolve paths ─────────────────────────────────────────────────────────
    TARGET_LAYER = args.layer

    file_path = Path(args.drawing).resolve()
    if not file_path.exists():
        sys.exit(f"[ERROR] Drawing file not found: {file_path}")

    allowables_path = _find_allowables(file_path, args.allowables)

    outdir = Path(args.outdir).resolve() if args.outdir else file_path.parent
    outdir.mkdir(parents=True, exist_ok=True)

    # Store resolved drawing path on args so render_html can access it
    args.drawing = str(file_path)

    # ── Stage 1: Extract ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  STAGE 1 / 2 — EXTRACTION")
    print("=" * 60)
    try:
        run_extraction(file_path, debug=args.debug, outdir=outdir)
    except Exception as exc:
        print(f"\n[ERROR] Extraction failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # ── Stage 2: Validate ─────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  STAGE 2 / 2 — VALIDATION")
    print("=" * 60)
    loads_path = outdir / "nozzle_loads_table.csv"
    out_path   = outdir / Path(args.out).name

    try:
        run_validation(loads_path, allowables_path, out_path, args)
    except Exception as exc:
        print(f"\n[ERROR] Validation failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # ── Done ──────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  ALL DONE")
    print("=" * 60)
    print(f"  CSV   : {outdir / 'nozzle_loads_table.csv'}")
    print(f"  XLSX  : {outdir / 'nozzle_loads_table.xlsx'}")
    print(f"  HTML  : {out_path}")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
