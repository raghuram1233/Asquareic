import ezdxf
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
from sklearn.cluster import DBSCAN
from sklearn.neighbors import NearestNeighbors
from collections import defaultdict, Counter
from tkinter import filedialog, Tk
from math import sqrt, pi, radians, atan2, degrees
import gc
import io
import os
import re
import sys

def find_oda_converter():
    import shutil
    from pathlib import Path
    exe = shutil.which("ODAFileConverter")
    if exe:
        return exe
    candidates = sorted(
        Path("C:/Program Files/ODA").glob("ODAFileConverter*/ODAFileConverter.exe"),
        reverse=True
    )
    return str(candidates[0]) if candidates else None


# ---------------------------------------------------------------------------
# File selection
# ---------------------------------------------------------------------------

def select_file():
    Tk().withdraw()
    return filedialog.askopenfilename(filetypes=[("DWG/DXF files", "*.dwg *.dxf")])


def load_doc(path):
    if path.lower().endswith('.dwg'):
        from ezdxf.addons import odafc
        oda_path = find_oda_converter()
        if not oda_path:
            raise RuntimeError("ODAFileConverter not found. Please install it or make sure it is on PATH.")
        odafc.win_exec_path = oda_path
        return odafc.readfile(path)
    return ezdxf.readfile(path)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def dist(p1, p2):
    return sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)


def line_angle_deg(p1, p2):
    angle = degrees(atan2(p2[1] - p1[1], p2[0] - p1[0])) % 180
    return angle


def point_to_segment_dist(p, p1, p2):
    x, y = p[0], p[1]
    x1, y1 = p1[0], p1[1]
    x2, y2 = p2[0], p2[1]
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return dist(p, p1)
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return dist((x, y), (x1 + t * dx, y1 + t * dy))


def is_aligned(text_rot, line_angle, tol_deg=25):
    diff = abs(text_rot - line_angle) % 180
    return min(diff, 180 - diff) <= tol_deg


# ---------------------------------------------------------------------------
# Extraction — single pass over msp to minimise memory allocation
# ---------------------------------------------------------------------------

def extract_all(msp):
    """Iterate modelspace once and return all geometry as plain Python floats.
    Using tuples of floats (not Vec3 objects) keeps per-item memory low."""
    dims, lines, circles, arcs, scale_texts = [], [], [], [], []

    for e in msp:
        t = e.dxftype()
        try:
            if t == "TEXT":
                raw = e.dxf.text
                clean = raw.replace("mm", "").replace("°", "").replace(" ", "")
                val = float(clean)
                rot = float(e.dxf.rotation) if e.dxf.hasattr("rotation") else 0.0
                ix, iy = float(e.dxf.insert.x), float(e.dxf.insert.y)
                dims.append((ix, iy, val, raw, rot))
                m = re.search(r"scale\s*[:=\-]?\s*1[:=\-]?(\d+)", raw, re.IGNORECASE)
                if m:
                    scale_texts.append((ix, iy, int(m.group(1))))

            elif t == "MTEXT":
                raw = e.plain_text()
                clean = raw.replace("mm", "").replace("°", "").replace(" ", "")
                val = float(clean)
                rot = float(e.dxf.rotation) if e.dxf.hasattr("rotation") else 0.0
                ix, iy = float(e.dxf.insert.x), float(e.dxf.insert.y)
                dims.append((ix, iy, val, raw, rot))
                m = re.search(r"scale\s*[:=\-]?\s*1[:=\-]?(\d+)", raw, re.IGNORECASE)
                if m:
                    scale_texts.append((ix, iy, int(m.group(1))))

            elif t == "DIMENSION":
                txt = e.dxf.text_override if e.dxf.hasattr("text_override") else ""
                val = float(txt.replace("mm", "").strip()) if txt else float(e.measurement)
                rot = float(e.dxf.rotation) if e.dxf.hasattr("rotation") else 0.0
                dx, dy = float(e.dxf.defpoint.x), float(e.dxf.defpoint.y)
                dims.append((dx, dy, val, str(val), rot))

            elif t == "LINE":
                s, en = e.dxf.start, e.dxf.end
                p1 = (float(s.x), float(s.y))
                p2 = (float(en.x), float(en.y))
                length = dist(p1, p2)
                if length > 0.01:
                    lines.append((p1, p2, length))

            elif t == "CIRCLE":
                c = e.dxf.center
                circles.append(((float(c.x), float(c.y)), float(e.dxf.radius)))

            elif t == "ARC":
                c = e.dxf.center
                radius = float(e.dxf.radius)
                angle = float(e.dxf.end_angle) - float(e.dxf.start_angle)
                if angle < 0:
                    angle += 360
                arc_len = radians(angle) * radius
                arcs.append(((float(c.x), float(c.y)), angle, arc_len, radius))

        except Exception:
            continue

    return dims, lines, circles, arcs, scale_texts


# ---------------------------------------------------------------------------
# Scale inference
# ---------------------------------------------------------------------------

STANDARD_SCALES = [0.02, 0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 500, 1000]


def snap_to_standard_scale(scale):
    if scale is None:
        return scale
    closest = min(STANDARD_SCALES, key=lambda s: abs(scale - s) / s)
    if abs(scale - closest) / closest <= 0.12:
        return closest
    return round(scale, 2)


def filter_overmatched(matches, max_per_element=2):
    """Remove matches where more than max_per_element texts point to the same CAD element."""
    counts = Counter((m[6], round(m[2], 1)) for m in matches)
    return [m for m in matches if counts[(m[6], round(m[2], 1))] <= max_per_element]


def detect_bom_labels(dims, min_seq=4):
    """Return indices of dims whose integer values form a consecutive run (BOM item numbers)."""
    int_dims = [(i, int(d[2])) for i, d in enumerate(dims)
                if d[2] >= 1 and d[2] == int(d[2])]
    if not int_dims:
        return set()

    int_dims.sort(key=lambda x: x[1])
    flagged = set()
    run = [int_dims[0]]

    for k in range(1, len(int_dims)):
        if int_dims[k][1] - int_dims[k - 1][1] == 1:
            run.append(int_dims[k])
        else:
            if len(run) >= min_seq:
                flagged.update(item[0] for item in run)
            run = [int_dims[k]]

    if len(run) >= min_seq:
        flagged.update(item[0] for item in run)

    return flagged


def infer_scale(matches):
    if not matches:
        return None
    ratios = []
    for _, val, cad_len, *_ in matches:
        if val < 10 or cad_len <= 0:
            continue
        ratios.append(val / cad_len)

    if not ratios:
        return None

    ratios = sorted(ratios)
    best_scale = None
    max_count = 0
    rel_tol = 0.05

    for r in ratios:
        count = sum(1 for x in ratios if abs(x - r) / r <= rel_tol)
        if count > max_count:
            max_count = count
            matching_ratios = [x for x in ratios if abs(x - r) / r <= rel_tol]
            best_scale = sum(matching_ratios) / len(matching_ratios)

    if max_count >= 2:
        return snap_to_standard_scale(best_scale)
    return None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_dimensions(dims, lines, circles, arcs, tol=500, scale_hint=None):
    matches = []
    for idx, (x, y, val, raw_text, rot) in enumerate(dims, 1):
        best_aligned, best_aligned_dist, aligned_type = None, float("inf"), None
        best_unaligned, best_unaligned_dist, unaligned_type = None, float("inf"), None

        for p1, p2, ln in lines:
            d = point_to_segment_dist((x, y), p1, p2)
            if d <= tol:
                if scale_hint:
                    ratio = val / ln if ln > 0 else 0
                    if not (scale_hint / 3 <= ratio <= scale_hint * 3):
                        continue
                angle = line_angle_deg(p1, p2)
                if is_aligned(rot, angle):
                    if d < best_aligned_dist:
                        best_aligned, best_aligned_dist, aligned_type = ln, d, 'line'
                else:
                    if d < best_unaligned_dist:
                        best_unaligned, best_unaligned_dist, unaligned_type = ln, d, 'line'

        for center, radius in circles:
            d = dist((x, y), center)
            if d <= tol:
                diameter = 2 * radius
                if scale_hint:
                    ratio = val / diameter if diameter > 0 else 0
                    if not (scale_hint / 3 <= ratio <= scale_hint * 3):
                        continue
                if d < best_aligned_dist:
                    best_aligned, best_aligned_dist, aligned_type = diameter, d, 'circle'

        for center, angle, arc_len, radius in arcs:
            d = dist((x, y), center)
            if d <= tol:
                if scale_hint and arc_len > 0:
                    ratio = val / arc_len
                    if not (scale_hint / 3 <= ratio <= scale_hint * 3):
                        continue
                if d < best_aligned_dist:
                    best_aligned, best_aligned_dist, aligned_type = arc_len, d, 'arc'

        if best_aligned is not None:
            best, best_dist, match_type = best_aligned, best_aligned_dist, aligned_type
        elif best_unaligned is not None:
            best, best_dist, match_type = best_unaligned, best_unaligned_dist, unaligned_type
        else:
            best, best_dist, match_type = None, None, None

        if best is not None:
            computed_scale = val / best if best else 0
            matches.append((idx, val, best, computed_scale, x, y, match_type, raw_text, rot))
    return matches


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster(matches):
    if not matches:
        return {}
    pts = np.array([[m[4], m[5]] for m in matches])
    if len(pts) < 3:
        return {0: matches}

    dists = NearestNeighbors(n_neighbors=min(4, len(pts))).fit(pts).kneighbors(pts)[0][:, -1]
    eps = max(np.median(dists) * 2, 200)
    labels = DBSCAN(eps=eps, min_samples=2).fit_predict(pts)

    out = defaultdict(list)
    for lab, m in zip(labels, matches):
        if lab == -1:
            continue
        out[lab].append(m)

    if not out:
        return {0: matches}
    return out


# ---------------------------------------------------------------------------
# Scale resolution per cluster
# ---------------------------------------------------------------------------

def resolve_scale(group, scale_texts, all_matches):
    if any(re.search(r"nt\s*s", m[7], re.IGNORECASE) for m in group):
        return None, "NTS (drawing note)"

    if scale_texts:
        cx = np.mean([m[4] for m in group])
        cy = np.mean([m[5] for m in group])
        closest = min(scale_texts, key=lambda s: dist((cx, cy), (s[0], s[1])))
        return closest[2], f"explicit text (1:{closest[2]})"

    inferred = infer_scale(group)
    if inferred:
        return inferred, f"inferred from cluster ratios (1:{inferred})"

    inferred_global = infer_scale(all_matches)
    if inferred_global:
        return inferred_global, f"inferred globally (1:{inferred_global})"

    return None, "unknown"


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

def _compute_bounds(msp):
    """Return (xmin, xmax, ymin, ymax) using ezdxf bbox, with coordinate-scan fallback."""
    try:
        from ezdxf import bbox as dxf_bbox
        ext = dxf_bbox.extents(msp, fast=True)
        if ext.has_data:
            return ext.extmin.x, ext.extmax.x, ext.extmin.y, ext.extmax.y
    except Exception:
        pass

    all_x, all_y = [], []
    for e in msp:
        try:
            t = e.dxftype()
            if t == "LINE":
                all_x += [e.dxf.start[0], e.dxf.end[0]]
                all_y += [e.dxf.start[1], e.dxf.end[1]]
            elif t in ("CIRCLE", "ARC"):
                cx, cy, r = e.dxf.center[0], e.dxf.center[1], e.dxf.radius
                all_x += [cx - r, cx + r]; all_y += [cy - r, cy + r]
            elif t == "LWPOLYLINE":
                for pt in e.get_points():
                    all_x.append(pt[0]); all_y.append(pt[1])
        except Exception:
            pass
    if all_x:
        return (np.percentile(all_x, 2), np.percentile(all_x, 98),
                np.percentile(all_y, 2), np.percentile(all_y, 98))
    return None


def visualize(doc, clusters, scale_map, scale_source_map, save_path=None):
    msp = doc.modelspace()
    bounds = _compute_bounds(msp)

    # ── Render DXF once to an in-memory PNG ──────────────────────────────────
    # Converts thousands of vector artists into a single raster image so that
    # all pan/zoom operations are instant (GPU image scaling, not artist redraw).
    print("  Rendering drawing to image...", end=" ", flush=True)
    rf, ra = plt.subplots(figsize=(32, 23), dpi=600)
    rf.patch.set_facecolor('#12121a')
    ra.set_facecolor('#12121a')
    ra.set_axis_off()
    Frontend(RenderContext(doc), MatplotlibBackend(ra)).draw_layout(msp)
    if bounds:
        xmin, xmax, ymin, ymax = bounds
        mx = (xmax - xmin) * 0.05 or 1
        my = (ymax - ymin) * 0.05 or mx
        ra.set_xlim(xmin - mx, xmax + mx)
        ra.set_ylim(ymin - my, ymax + my)
    ra.set_aspect('auto')
    buf = io.BytesIO()
    rf.savefig(buf, format='png', dpi=600, facecolor='#12121a', bbox_inches='tight')
    plt.close(rf)
    buf.seek(0)
    img = plt.imread(buf)
    buf.close()
    gc.collect()
    print("done")

    # ── Interactive figure with image + badge overlays ────────────────────────
    fig, ax = plt.subplots(figsize=(18, 13))
    fig.patch.set_facecolor('#12121a')
    ax.set_facecolor('#12121a')
    ax.set_axis_off()

    if bounds:
        xmin, xmax, ymin, ymax = bounds
        mx = (xmax - xmin) * 0.05 or 1
        my = (ymax - ymin) * 0.05 or mx
        extent = [xmin - mx, xmax + mx, ymin - my, ymax + my]
    else:
        extent = [0, img.shape[1], 0, img.shape[0]]

    # Single imshow — the only object that needs redrawing on pan/zoom
    ax.imshow(img, extent=extent, aspect='auto', origin='upper',
              interpolation='antialiased', zorder=0)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])

    initial_width = extent[1] - extent[0]
    BASE_FONT_PT = 9.0

    for cid, group in sorted(clusters.items()):
        if not group:
            continue
        ref, _ = scale_map.get(cid, (None, "unknown"))
        for mid, val, cad_val, scale_ratio, x, y, typ, txt, rot in group:
            expected = cad_val * ref if ref else val
            ok = ref is not None and abs(val - expected) / max(val, 0.01) <= 0.10
            label = f"{val:.0f}" + ("" if ok else f" -> {expected:.0f}")
            fc = '#1a3a27' if ok else '#3a1a1f'
            ec = '#28a745' if ok else '#dc3545'
            tc = '#5cb85c' if ok else '#e06c75'
            offset = initial_width * 0.018
            is_vert = 65 <= (rot % 180) <= 115
            ax.text(x + (offset if is_vert else 0),
                    y + (0 if is_vert else offset),
                    label, color=tc, fontsize=BASE_FONT_PT,
                    fontweight='normal' if ok else 'bold',
                    ha='center', va='center', zorder=5,
                    bbox=dict(boxstyle='round,pad=0.3', fc=fc, ec=ec,
                              lw=1.2, alpha=0.92))

    # ── Scroll-wheel zoom ─────────────────────────────────────────────────────
    def on_scroll(event):
        if event.inaxes != ax:
            return
        factor = 0.85 if event.button == 'up' else 1.0 / 0.85
        cx, cy = event.xdata, event.ydata
        if cx is None:
            return
        xl, xr = ax.get_xlim()
        yb, yt = ax.get_ylim()
        ax.set_xlim(cx + (xl - cx) * factor, cx + (xr - cx) * factor)
        ax.set_ylim(cy + (yb - cy) * factor, cy + (yt - cy) * factor)
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect('scroll_event', on_scroll)

    # ── Middle-mouse pan ──────────────────────────────────────────────────────
    _pan = {'active': False, 'x': None, 'y': None}

    def on_press(event):
        if event.button == 2 and event.inaxes == ax:
            _pan.update(active=True, x=event.xdata, y=event.ydata)

    def on_release(event):
        if event.button == 2:
            _pan['active'] = False

    def on_motion(event):
        if not _pan['active'] or event.inaxes != ax or event.xdata is None:
            return
        dx = _pan['x'] - event.xdata
        dy = _pan['y'] - event.ydata
        xl, xr = ax.get_xlim()
        yb, yt = ax.get_ylim()
        ax.set_xlim(xl + dx, xr + dx)
        ax.set_ylim(yb + dy, yt + dy)
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect('button_press_event', on_press)
    fig.canvas.mpl_connect('button_release_event', on_release)
    fig.canvas.mpl_connect('motion_notify_event', on_motion)

    plt.tight_layout(pad=0)
    if save_path:
        plt.savefig(save_path, dpi=150, facecolor='#12121a')
        plt.close(fig)
        print(f"  Saved visualization to: {save_path}")
    else:
        plt.show()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    save_path = None
    if "--save-plot" in sys.argv:
        try:
            idx = sys.argv.index("--save-plot")
            save_path = sys.argv[idx + 1]
            sys.argv.pop(idx + 1)
            sys.argv.pop(idx)
        except Exception:
            pass

    path = sys.argv[1] if len(sys.argv) > 1 else select_file()
    if not path:
        return

    print(f"\nReading: {os.path.basename(path)}")
    doc = load_doc(path)
    msp = doc.modelspace()

    dims, lines, circles, arcs, scale_texts = extract_all(msp)
    gc.collect()

    print(f"  Found {len(dims)} dimension texts, {len(lines)} lines, "
          f"{len(circles)} circles, {len(arcs)} arcs, "
          f"{len(scale_texts)} scale annotations")

    bom_indices = detect_bom_labels(dims)
    if bom_indices:
        print(f"  Filtered {len(bom_indices)} BOM label texts (sequential item numbers)")
    dims = [d for i, d in enumerate(dims) if i not in bom_indices]

    # --- Step 1: Rough match to infer global scale ---
    median_line_len = float(np.median([ln for _, _, ln in lines])) if lines else 100
    rough_tol = max(50, median_line_len * 0.25)
    rough_matches = match_dimensions(dims, lines, circles, arcs, tol=rough_tol)
    rough_matches_filtered = filter_overmatched(rough_matches)
    global_scale = infer_scale(rough_matches_filtered)
    if global_scale:
        print(f"  Auto-inferred global scale: 1:{global_scale}")
    elif scale_texts:
        print(f"  Scale from drawing text: 1:{scale_texts[0][2]}")
    else:
        print("  No scale found — will infer per cluster")

    # --- Step 2: Best tolerance search with scale gate ---
    max_tol = max(100, int(median_line_len * 0.3))
    step = max(2, max_tol // 12)
    best_tol, best_score, best_result = None, -1, None
    for tol in range(2, max_tol + 1, step):
        matches = match_dimensions(dims, lines, circles, arcs, tol, scale_hint=global_scale)
        cl = cluster(matches)
        score = 0
        for group in cl.values():
            ref, _ = resolve_scale(group, scale_texts, matches)
            score += sum(
                1 for m in group
                if ref and abs(m[1] - m[2] * ref) / max(m[1], 0.01) <= 0.10
            )
        if score > best_score:
            best_score, best_result, best_tol = score, (matches, cl), tol

    print(f"\n  Best tolerance: {best_tol}  |  Matched: {best_score} dimensions")

    matches, clusters = best_result
    matches = filter_overmatched(matches)
    clusters = cluster(matches)

    # --- Step 3: Build scale map ---
    scale_map = {}
    scale_source_map = {}
    for cid, group in clusters.items():
        ref, source = resolve_scale(group, scale_texts, matches)
        scale_map[cid] = (ref, source)
        scale_source_map[cid] = source

    # --- Step 4: Console report ---
    for i, (cid, group) in enumerate(sorted(clusters.items()), 1):
        ref, source = scale_map.get(cid, (None, "unknown"))
        print(f"\nDesign {i}  (Scale: 1:{ref if ref else 'NTS'}  |  {source})")
        for mid, val, cad_len, sc, x, y, dtype, raw_txt, rot in group:
            expected = cad_len * ref if ref else val
            delta_rel = abs(val - expected) / max(val, 0.01) if ref else 1.0
            status = "OK" if delta_rel <= 0.10 else f"Suggest -> {expected:.1f}"
            print(f"  [{mid:02}] Dim:{val:.1f}  CAD:{cad_len:.2f}  Type:{dtype}  -> {status}")

    # --- Step 5: Visualize ---
    visualize(doc, clusters, scale_map, scale_source_map, save_path=save_path)


if __name__ == "__main__":
    main()
