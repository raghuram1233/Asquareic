"""
Image Comparator
=================

Production-grade image-based CAD view comparison pipeline.

Instead of trying to normalize CAD coordinate systems mathematically,
this module:
    1. Renders each view's geometry (model & drawing) as a rasterized image
    2. Detects the occupied object region via thresholding
    3. Crops tightly around the object
    4. Resizes both cropped images to the SAME canvas
    5. Center-aligns the objects
    6. Overlays directly for visual comparison

This approach is robust against:
    - Viewport padding differences
    - Scaling inconsistencies
    - Projection extent variations
    - Export coordinate system mismatches
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from .geometry_store import Edge2D, ViewGeometry, ViewName

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

# Render settings
DEFAULT_RENDER_SIZE = 2048        # px — high-res for thin CAD lines
DEFAULT_LINE_THICKNESS = 2        # px — visible but not bloated
DEFAULT_ANTI_ALIAS = cv2.LINE_AA  # anti-aliased lines

# Detection settings
DEFAULT_THRESHOLD = 10            # intensity threshold for "geometry pixel"
DEFAULT_MORPH_KERNEL = 5          # px — for closing small gaps between edges
DEFAULT_MORPH_ITERATIONS = 3      # iterations of morphological closing

# Crop / resize settings
DEFAULT_MARGIN_RATIO = 0.03       # 3% margin around the cropped object
DEFAULT_CANVAS_SIZE = 1024        # px — final overlay canvas (square)

# Comparison colors (BGR for OpenCV)
COLOR_MODEL = (0, 240, 255)       # yellow — model projection edges
COLOR_DRAWING = (255, 180, 80)    # light blue — drawing edges
COLOR_MATCH = (80, 255, 80)       # green — overlap regions
COLOR_MODEL_ONLY = (0, 0, 255)    # red — model-only pixels
COLOR_DRAWING_ONLY = (255, 100, 0)  # blue — drawing-only pixels


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class CropInfo:
    """Metadata about a tight crop operation."""
    original_size: Tuple[int, int]   # (H, W) before crop
    bbox: Tuple[int, int, int, int]  # (x, y, w, h) detected bounding box
    margin_px: int                   # margin added around bbox
    crop_region: Tuple[int, int, int, int]  # (y1, y2, x1, x2) actual crop slice
    object_aspect_ratio: float       # width / height of the object


@dataclass
class OverlayResult:
    """Result of a single view's image-based comparison."""
    view_name: ViewName
    model_image: np.ndarray           # cropped & resized model image
    drawing_image: np.ndarray         # cropped & resized drawing image
    overlay_image: np.ndarray         # final blended overlay
    diff_image: np.ndarray            # color-coded diff visualization
    model_crop_info: CropInfo
    drawing_crop_info: CropInfo
    overlap_score: float              # 0.0–1.0 pixel overlap ratio
    model_coverage: float             # fraction of model pixels matched
    drawing_coverage: float           # fraction of drawing pixels matched
    canvas_size: Tuple[int, int]      # (H, W) of the comparison canvas


# ── Core Pipeline ─────────────────────────────────────────────────────────────

class ImageComparator:
    """
    Image-based CAD view comparison engine.

    Algorithm:
        1. render_geometry() → rasterize Edge2D list onto a high-res canvas
        2. detect_object_bbox() → threshold + morphology → bounding box
        3. crop_tight() → extract object region with margin
        4. resize_to_canvas() → scale to uniform size, preserving aspect ratio
        5. overlay() → blend + diff for visual comparison
    """

    def __init__(
        self,
        render_size: int = DEFAULT_RENDER_SIZE,
        line_thickness: int = DEFAULT_LINE_THICKNESS,
        threshold: int = DEFAULT_THRESHOLD,
        morph_kernel: int = DEFAULT_MORPH_KERNEL,
        morph_iterations: int = DEFAULT_MORPH_ITERATIONS,
        margin_ratio: float = DEFAULT_MARGIN_RATIO,
        canvas_size: int = DEFAULT_CANVAS_SIZE,
    ):
        self.render_size = render_size
        self.line_thickness = line_thickness
        self.threshold = threshold
        self.morph_kernel = morph_kernel
        self.morph_iterations = morph_iterations
        self.margin_ratio = margin_ratio
        self.canvas_size = canvas_size

    # ── Public API ────────────────────────────────────────────────────────

    def filter_outliers(self, edges: List[Edge2D], distance_factor: float = 0.18) -> List[Edge2D]:
        """Keep only edges belonging to the dominant spatial cluster to ignore stray DXF artifacts."""
        if len(edges) < 3:
            return edges

        centroids = np.array([edge.centroid for edge in edges])
        extents = centroids.max(axis=0) - centroids.min(axis=0)
        threshold = max(float(max(extents)), 1.0) * distance_factor

        parent = list(range(len(edges)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(len(centroids)):
            distances = np.linalg.norm(centroids[i + 1:] - centroids[i], axis=1)
            for offset in np.where(distances <= threshold)[0]:
                union(i, i + 1 + int(offset))

        clusters: Dict[int, List[int]] = {}
        for i in range(len(edges)):
            clusters.setdefault(find(i), []).append(i)

        def cluster_weight(indices: List[int]) -> float:
            return sum(edges[i].length for i in indices)

        main_indices = max(clusters.values(), key=cluster_weight)
        
        filtered_edges = [edges[i] for i in main_indices]
        if len(filtered_edges) < len(edges):
            logger.debug("Filtered out %d outlier edges", len(edges) - len(filtered_edges))
            
        return filtered_edges

    def compare_view(
        self,
        model_geom: ViewGeometry,
        drawing_geom: ViewGeometry,
    ) -> OverlayResult:
        """
        Full comparison pipeline for a single orthographic view.

        Args:
            model_geom:   3D model's projected 2D edges for this view.
            drawing_geom: 2D CAD drawing edges for this view.

        Returns:
            OverlayResult with all comparison images and metrics.
        """
        view_name = model_geom.view_name

        logger.info("ImageComparator: processing view %s", view_name.value)

        # ── Step 0: Filter outliers ───────────────────────────────────
        # Use every extracted edge for final comparison. The DXF splitter now
        # removes borders/dimensions before this point, so dominant-cluster
        # filtering would hide valid separated details in the final diff.
        model_edges_clean = model_geom.edges
        drawing_edges_clean = drawing_geom.edges

        # ── Step 1: Render both geometries to images ──────────────────
        model_raw = self.render_geometry(
            model_edges_clean,
            color=COLOR_MODEL,
            label="model",
        )
        drawing_raw = self.render_geometry(
            drawing_edges_clean,
            color=COLOR_DRAWING,
            label="drawing",
        )

        # ── Step 2: Detect object bounding boxes ─────────────────────
        model_bbox, model_mask = self.detect_object_bbox(model_raw)
        drawing_bbox, drawing_mask = self.detect_object_bbox(drawing_raw)

        if model_bbox is None or drawing_bbox is None:
            logger.warning(
                "View %s: empty geometry detected (model_bbox=%s, drawing_bbox=%s)",
                view_name.value, model_bbox, drawing_bbox,
            )
            # Return blank overlay
            blank = np.zeros((self.canvas_size, self.canvas_size, 3), dtype=np.uint8)
            crop_info = CropInfo(
                original_size=(self.render_size, self.render_size),
                bbox=(0, 0, 0, 0),
                margin_px=0,
                crop_region=(0, 0, 0, 0),
                object_aspect_ratio=1.0,
            )
            return OverlayResult(
                view_name=view_name,
                model_image=blank.copy(),
                drawing_image=blank.copy(),
                overlay_image=blank.copy(),
                diff_image=blank.copy(),
                model_crop_info=crop_info,
                drawing_crop_info=crop_info,
                overlap_score=0.0,
                model_coverage=0.0,
                drawing_coverage=0.0,
                canvas_size=(self.canvas_size, self.canvas_size),
            )

        # ── Step 3: Crop tightly around objects ──────────────────────
        model_cropped, model_crop_info = self.crop_tight(model_raw, model_bbox)
        drawing_cropped, drawing_crop_info = self.crop_tight(drawing_raw, drawing_bbox)

        logger.debug(
            "  Model crop: %s → %s, Drawing crop: %s → %s",
            model_raw.shape[:2], model_cropped.shape[:2],
            drawing_raw.shape[:2], drawing_cropped.shape[:2],
        )

        # ── Step 4: Resize both to identical canvas ──────────────────
        model_resized = self.resize_to_canvas(model_cropped)
        drawing_resized = self.resize_to_canvas(drawing_cropped)

        # ── Step 5: Generate overlay and diff ────────────────────────
        overlay_img = self.create_overlay(model_resized, drawing_resized)
        diff_img, overlap_score, model_cov, drawing_cov = self.create_diff(
            model_resized, drawing_resized
        )

        logger.info(
            "  View %s: overlap=%.1f%%, model_cov=%.1f%%, drawing_cov=%.1f%%",
            view_name.value,
            overlap_score * 100,
            model_cov * 100,
            drawing_cov * 100,
        )

        return OverlayResult(
            view_name=view_name,
            model_image=model_resized,
            drawing_image=drawing_resized,
            overlay_image=overlay_img,
            diff_image=diff_img,
            model_crop_info=model_crop_info,
            drawing_crop_info=drawing_crop_info,
            overlap_score=overlap_score,
            model_coverage=model_cov,
            drawing_coverage=drawing_cov,
            canvas_size=(self.canvas_size, self.canvas_size),
        )

    def compare_all_views(
        self,
        model_views: Dict[ViewName, ViewGeometry],
        drawing_views: Dict[ViewName, ViewGeometry],
    ) -> Dict[ViewName, OverlayResult]:
        """Compare all common views between model and drawing."""
        results = {}
        for view_name in model_views:
            if view_name in drawing_views:
                results[view_name] = self.compare_view(
                    model_views[view_name],
                    drawing_views[view_name],
                )
            else:
                logger.warning("View %s: no drawing counterpart", view_name.value)
        return results

    # ── Step 1: Geometry → Image Rendering ────────────────────────────

    def render_geometry(
        self,
        edges: List[Edge2D],
        color: Tuple[int, int, int] = (255, 255, 255),
        label: str = "",
    ) -> np.ndarray:
        """
        Rasterize Edge2D entities onto a high-resolution image.

        The geometry coordinates are mapped to fill the render canvas
        with uniform scaling (preserving aspect ratio).

        Args:
            edges: List of Edge2D entities to render.
            color: BGR color for the edges.
            label: Label for logging.

        Returns:
            BGR image (render_size × render_size × 3) with edges drawn.
        """
        canvas = np.zeros(
            (self.render_size, self.render_size, 3), dtype=np.uint8
        )

        if not edges:
            return canvas

        # Collect all points to compute the world bounding box
        all_points = []
        for edge in edges:
            if len(edge.points) >= 2:
                all_points.append(edge.points)

        if not all_points:
            return canvas

        all_pts = np.vstack(all_points)
        min_xy = all_pts.min(axis=0)
        max_xy = all_pts.max(axis=0)
        extent = max_xy - min_xy

        # Avoid division by zero for degenerate geometry
        extent[extent < 1e-10] = 1.0

        # Compute scale to fit within canvas with a small internal margin
        internal_margin = self.render_size * 0.05  # 5% border
        usable_size = self.render_size - 2 * internal_margin
        scale = usable_size / max(extent[0], extent[1])

        # Compute offset to center the geometry
        scaled_extent = extent * scale
        offset_x = internal_margin + (usable_size - scaled_extent[0]) / 2
        offset_y = internal_margin + (usable_size - scaled_extent[1]) / 2

        # Draw each edge
        for edge in edges:
            if len(edge.points) < 2:
                continue

            # Transform world coords → pixel coords
            pts = edge.points.copy()
            pts = (pts - min_xy) * scale
            pts[:, 0] += offset_x
            pts[:, 1] += offset_y

            # Flip Y axis (image coords: Y goes down)
            pts[:, 1] = self.render_size - pts[:, 1]

            # Convert to integer pixel coordinates
            pixel_pts = pts.astype(np.int32).reshape(-1, 1, 2)

            # Draw the polyline
            cv2.polylines(
                canvas,
                [pixel_pts],
                isClosed=False,
                color=color,
                thickness=self.line_thickness,
                lineType=DEFAULT_ANTI_ALIAS,
            )

        n_edges = len([e for e in edges if len(e.points) >= 2])
        logger.debug("  Rendered %d edges (%s) at scale=%.2f", n_edges, label, scale)

        return canvas

    # ── Step 2: Object Detection via Thresholding ─────────────────────

    def detect_object_bbox(
        self, image: np.ndarray
    ) -> Tuple[Optional[Tuple[int, int, int, int]], Optional[np.ndarray]]:
        """
        Detect the bounding box of the geometry object in the rendered image.

        Strategy for CAD line drawings:
            1. Convert to grayscale
            2. Apply low threshold (catch thin/faint lines)
            3. Morphological closing (bridge disconnected CAD edges)
            4. Find bounding rect of all non-zero pixels

        Args:
            image: BGR rendered image.

        Returns:
            (bbox, mask) where bbox = (x, y, w, h) or (None, None) if empty.
        """
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

        # ── Thresholding ──────────────────────────────────────────────
        # Low threshold to catch thin CAD lines and anti-aliased edges
        _, binary = cv2.threshold(gray, self.threshold, 255, cv2.THRESH_BINARY)

        # ── Morphological Closing ─────────────────────────────────────
        # Bridges gaps between disconnected edges so they form a
        # cohesive region. Critical for sparse CAD line drawings.
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (self.morph_kernel, self.morph_kernel),
        )
        closed = cv2.morphologyEx(
            binary,
            cv2.MORPH_CLOSE,
            kernel,
            iterations=self.morph_iterations,
        )

        # ── Optional: Light dilation to catch thin line edges ─────────
        # This ensures 1-px thin lines aren't clipped at the bbox boundary
        dilate_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated = cv2.dilate(closed, dilate_kernel, iterations=1)

        # ── Find bounding box of all non-zero pixels ──────────────────
        coords = cv2.findNonZero(dilated)
        if coords is None:
            return None, None

        x, y, w, h = cv2.boundingRect(coords)

        # Sanity check: reject trivially small detections (noise)
        min_dim = max(image.shape[0], image.shape[1]) * 0.005
        if w < min_dim or h < min_dim:
            logger.warning(
                "Detected bbox too small (%dx%d), treating as empty", w, h
            )
            return None, None

        logger.debug(
            "  Object detected: bbox=(%d,%d,%d,%d), coverage=%.1f%%",
            x, y, w, h,
            (w * h) / (image.shape[0] * image.shape[1]) * 100,
        )

        return (x, y, w, h), binary

    # ── Step 3: Tight Crop with Margin ────────────────────────────────

    def crop_tight(
        self,
        image: np.ndarray,
        bbox: Tuple[int, int, int, int],
    ) -> Tuple[np.ndarray, CropInfo]:
        """
        Crop the image tightly around the detected object bbox,
        adding a small margin to avoid clipping thin edge lines.

        Args:
            image: Source image.
            bbox:  (x, y, w, h) from detect_object_bbox.

        Returns:
            (cropped_image, CropInfo)
        """
        x, y, w, h = bbox
        H, W = image.shape[:2]

        # Compute margin in pixels
        max_dim = max(w, h)
        margin_px = int(max_dim * self.margin_ratio)
        # Minimum margin of 4px to avoid clipping anti-aliased edges
        margin_px = max(margin_px, 4)

        # Apply margin with boundary clamping
        y1 = max(0, y - margin_px)
        y2 = min(H, y + h + margin_px)
        x1 = max(0, x - margin_px)
        x2 = min(W, x + w + margin_px)

        cropped = image[y1:y2, x1:x2].copy()

        crop_info = CropInfo(
            original_size=(H, W),
            bbox=bbox,
            margin_px=margin_px,
            crop_region=(y1, y2, x1, x2),
            object_aspect_ratio=w / max(h, 1),
        )

        return cropped, crop_info

    # ── Step 4: Aspect-Ratio Preserving Resize ────────────────────────

    def resize_to_canvas(
        self,
        image: np.ndarray,
        canvas_size: Optional[int] = None,
    ) -> np.ndarray:
        """
        Resize the cropped image to fit within a square canvas,
        preserving aspect ratio and centering the content.

        This is the critical step that ensures both images occupy the
        exact same pixel space for overlay comparison.

        Strategy:
            1. Compute scale factor to fit the largest dimension
            2. Resize with INTER_AREA (shrinking) or INTER_CUBIC (upscaling)
            3. Center-paste onto a black square canvas

        Args:
            image:       Cropped image to resize.
            canvas_size: Target canvas dimension (default: self.canvas_size).

        Returns:
            Square image (canvas_size × canvas_size × 3) with centered content.
        """
        target = canvas_size or self.canvas_size
        h, w = image.shape[:2]

        if h == 0 or w == 0:
            return np.zeros((target, target, 3), dtype=np.uint8)

        # Scale factor to fit the larger dimension
        scale = target / max(h, w)

        # Choose interpolation method based on direction
        if scale < 1.0:
            interp = cv2.INTER_AREA   # best for downscaling
        else:
            interp = cv2.INTER_CUBIC  # best for upscaling

        new_w = max(1, int(w * scale))
        new_h = max(1, int(h * scale))

        resized = cv2.resize(image, (new_w, new_h), interpolation=interp)

        # Create black canvas and center-paste
        canvas = np.zeros((target, target, 3), dtype=np.uint8)
        y_offset = (target - new_h) // 2
        x_offset = (target - new_w) // 2

        canvas[y_offset:y_offset + new_h, x_offset:x_offset + new_w] = resized

        return canvas

    # ── Step 5: Overlay Generation ────────────────────────────────────

    def create_overlay(
        self,
        model_img: np.ndarray,
        drawing_img: np.ndarray,
        alpha: float = 0.5,
    ) -> np.ndarray:
        """
        Create a blended overlay of the model and drawing images.

        Both images must be the same size (output of resize_to_canvas).

        Args:
            model_img:   Resized model image.
            drawing_img: Resized drawing image.
            alpha:       Blending weight (0.5 = equal blend).

        Returns:
            Blended BGR overlay image.
        """
        overlay = cv2.addWeighted(model_img, alpha, drawing_img, 1.0 - alpha, 0)
        return overlay

    def create_diff(
        self,
        model_img: np.ndarray,
        drawing_img: np.ndarray,
    ) -> Tuple[np.ndarray, float, float, float]:
        """
        Create a color-coded diff visualization and compute overlap metrics.

        Color coding:
            - Green:  pixels present in BOTH (overlap)
            - Red:    pixels in model ONLY (missing from drawing)
            - Blue:   pixels in drawing ONLY (extra in drawing)
            - Black:  background

        Args:
            model_img:   Resized model image.
            drawing_img: Resized drawing image.

        Returns:
            (diff_image, overlap_score, model_coverage, drawing_coverage)
        """
        # Convert to grayscale masks
        model_gray = cv2.cvtColor(model_img, cv2.COLOR_BGR2GRAY)
        drawing_gray = cv2.cvtColor(drawing_img, cv2.COLOR_BGR2GRAY)

        # Binary masks with same threshold
        _, model_mask = cv2.threshold(
            model_gray, self.threshold, 255, cv2.THRESH_BINARY
        )
        _, drawing_mask = cv2.threshold(
            drawing_gray, self.threshold, 255, cv2.THRESH_BINARY
        )

        # Apply light dilation to compensate for sub-pixel rendering differences
        # This is critical: CAD lines can be offset by 1-2 pixels due to
        # different anti-aliasing in the two rendering passes
        tolerance_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        model_dilated = cv2.dilate(model_mask, tolerance_kernel, iterations=1)
        drawing_dilated = cv2.dilate(drawing_mask, tolerance_kernel, iterations=1)

        # Compute overlap regions using dilated masks for tolerance
        both = cv2.bitwise_and(model_dilated, drawing_dilated)
        model_only = cv2.bitwise_and(model_mask, cv2.bitwise_not(drawing_dilated))
        drawing_only = cv2.bitwise_and(drawing_mask, cv2.bitwise_not(model_dilated))

        # Build color-coded diff image
        diff = np.zeros_like(model_img)
        diff[both > 0] = COLOR_MATCH          # green — overlap
        diff[model_only > 0] = COLOR_MODEL_ONLY    # red — model only
        diff[drawing_only > 0] = COLOR_DRAWING_ONLY  # blue — drawing only

        # Compute metrics
        model_pixels = np.count_nonzero(model_mask)
        drawing_pixels = np.count_nonzero(drawing_mask)
        overlap_pixels = np.count_nonzero(both)

        # Overlap score: Jaccard-like IoU
        union_pixels = np.count_nonzero(
            cv2.bitwise_or(model_mask, drawing_mask)
        )
        overlap_score = overlap_pixels / max(union_pixels, 1)

        # Coverage: what fraction of each source is matched
        model_coverage = overlap_pixels / max(model_pixels, 1)
        drawing_coverage = overlap_pixels / max(drawing_pixels, 1)

        return diff, overlap_score, model_coverage, drawing_coverage

    # ── Utility: Save Debug Images ────────────────────────────────────

    def save_debug_images(
        self,
        result: OverlayResult,
        output_dir: Path,
    ):
        """Save all intermediate comparison images for debugging."""
        output_dir.mkdir(parents=True, exist_ok=True)
        vn = result.view_name.value

        cv2.imwrite(
            str(output_dir / f"img_{vn}_model_cropped.png"),
            result.model_image,
        )
        cv2.imwrite(
            str(output_dir / f"img_{vn}_drawing_cropped.png"),
            result.drawing_image,
        )
        cv2.imwrite(
            str(output_dir / f"img_{vn}_overlay.png"),
            result.overlay_image,
        )
        cv2.imwrite(
            str(output_dir / f"img_{vn}_diff.png"),
            result.diff_image,
        )

        logger.info("Saved debug images for view %s to %s", vn, output_dir)

    def save_all_debug_images(
        self,
        results: Dict[ViewName, OverlayResult],
        output_dir: Path,
    ):
        """Save debug images for all views."""
        for result in results.values():
            self.save_debug_images(result, output_dir)

    # ── Utility: Composite Grid ───────────────────────────────────────

    def create_comparison_grid(
        self,
        results: Dict[ViewName, OverlayResult],
        cell_size: int = 512,
    ) -> np.ndarray:
        """
        Create a grid image showing all views' comparisons.

        Layout: 6 rows (one per view) × 4 columns
            [Model | Drawing | Overlay | Diff]

        Args:
            results:   Per-view overlay results.
            cell_size: Size of each cell in the grid.

        Returns:
            Large composite BGR image.
        """
        view_order = [
            ViewName.FRONT, ViewName.BACK,
            ViewName.TOP, ViewName.BOTTOM,
            ViewName.LEFT, ViewName.RIGHT,
        ]

        n_cols = 4
        n_rows = len(view_order)
        header_height = 40

        grid = np.zeros(
            (n_rows * (cell_size + header_height) + header_height,
             n_cols * cell_size,
             3),
            dtype=np.uint8,
        )

        # Column headers
        col_labels = ["3D Model", "2D Drawing", "Overlay", "Diff"]
        for c, label in enumerate(col_labels):
            x = c * cell_size + cell_size // 2 - len(label) * 6
            cv2.putText(
                grid, label,
                (max(x, 5), 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2,
                cv2.LINE_AA,
            )

        for r, view_name in enumerate(view_order):
            y_base = r * (cell_size + header_height) + header_height

            # Row label
            label = f"{view_name.value.upper()}"
            if view_name in results:
                res = results[view_name]
                label += f"  [{res.overlap_score:.0%}]"
                images = [
                    res.model_image,
                    res.drawing_image,
                    res.overlay_image,
                    res.diff_image,
                ]
            else:
                images = [None] * 4

            cv2.putText(
                grid, label,
                (10, y_base + 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (180, 180, 180), 2,
                cv2.LINE_AA,
            )

            for c, img in enumerate(images):
                if img is None:
                    continue
                x_base = c * cell_size

                # Resize to cell
                cell = cv2.resize(
                    img, (cell_size, cell_size),
                    interpolation=cv2.INTER_AREA,
                )
                grid[
                    y_base + header_height: y_base + header_height + cell_size,
                    x_base: x_base + cell_size,
                ] = cell

        return grid
