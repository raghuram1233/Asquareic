"""
Pipeline
========

End-to-end orchestrator that wires together all modules:
    STEP → Projection → HLR → Edge Extraction → DXF Parsing →
    Normalization → Matching → Diff Detection → Reporting
"""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, List, Optional

from .geometry_store import ValidationReport, ViewName
from .step_loader import STEPLoader
from .projector import Projector
from .hlr_engine import HLREngine
from .edge_extractor import EdgeExtractor
from .dxf_parser import DXFParser
from .normalizer import Normalizer
from .matcher import Matcher
from .diff_detector import DiffDetector
from .reporter import Reporter
from .image_comparator import ImageComparator

logger = logging.getLogger(__name__)


class PipelineConfig:
    """Configuration for the validation pipeline."""

    def __init__(
        self,
        # Edge extraction
        deflection: float = 0.01,
        include_hidden: bool = False,
        # DXF parsing
        # (Parser now strictly operates on 1 file per view)
        drawing_scale: float = 1.0,
        drawing_view_rotations: Optional[Dict[ViewName, float]] = None,

        # Matching
        match_distance_threshold: float = 0.05,
        match_bbox_expansion: float = 0.1,
        # Diff detection
        modification_threshold: float = 0.15,
        min_edge_length: float = 0.005,
        # Which views to process
        views: Optional[List[ViewName]] = None,
        # Output
        output_dir: str = "output",
    ):
        self.deflection = deflection
        self.include_hidden = include_hidden
        self.drawing_scale = drawing_scale
        self.drawing_view_rotations = drawing_view_rotations or {}


        self.match_distance_threshold = match_distance_threshold
        self.match_bbox_expansion = match_bbox_expansion
        self.modification_threshold = modification_threshold
        self.min_edge_length = min_edge_length
        self.views = views or list(ViewName)
        self.output_dir = output_dir


class Pipeline:
    """
    Orchestrates the full CAD validation pipeline.

    Usage:
        config = PipelineConfig(output_dir="results")
        pipeline = Pipeline(config)
        report = pipeline.run("model.step", "drawing.dxf")
        # Or with multiple drawings for each view:
        # report = pipeline.run("model.step", {ViewName.FRONT: "front.dxf", ViewName.TOP: "top.dxf"})
    """

    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()

    def run(
        self,
        step_file: str,
        drawing_file: Dict[ViewName, str],
    ) -> ValidationReport:
        """
        Execute the full validation pipeline.

        Args:
            step_file: Path to the 3D STEP model.
            drawing_file: A dictionary mapping ViewName to paths of 
                          individual drawing files for each view.

        Returns:
            A ValidationReport with per-view results.
        """
        t_start = time.time()
        logger.info("=" * 60)
        logger.info("CAD VALIDATION PIPELINE — START")
        logger.info("STEP: %s", step_file)
        logger.info("DRAWING: Multiple files provided (%d views)", len(drawing_file))
        logger.info("=" * 60)

        # ── Step 1: Load STEP ─────────────────────────────────────
        logger.info("── Step 1/8: Loading STEP file ──")
        t = time.time()
        loader = STEPLoader(step_file)
        shape = loader.load(heal=True)
        shape_info = loader.get_shape_info()
        loader.validate()
        logger.info("  STEP loaded in %.2fs — %d faces, %d edges",
                     time.time() - t,
                     shape_info.get("faces", 0),
                     shape_info.get("edges", 0))

        # ── Step 2: Create projectors ─────────────────────────────
        logger.info("── Step 2/8: Creating projectors ──")
        t = time.time()
        proj = Projector(shape)
        # Build every standard model view for reporting; validation can still
        # be limited to self.config.views.
        all_projectors = proj.create_all_projectors()
        logger.info("  Created %d projectors in %.2fs",
                     len(all_projectors), time.time() - t)

        # ── Step 3: Hidden line removal ───────────────────────────
        logger.info("── Step 3/8: Hidden line removal ──")
        t = time.time()
        hlr = HLREngine(shape)
        hlr_results_all = hlr.compute_all(all_projectors)
        hlr_results = {
            v: hlr_results_all[v] for v in self.config.views if v in hlr_results_all
        }
        logger.info("  HLR completed in %.2fs", time.time() - t)

        # ── Step 4: Edge extraction ───────────────────────────────
        logger.info("── Step 4/8: Extracting 2D edges ──")
        t = time.time()
        extractor = EdgeExtractor(deflection=self.config.deflection)
        model_views_all = extractor.extract_all(
            hlr_results_all, include_hidden=self.config.include_hidden
        )
        model_views = {
            v: model_views_all[v] for v in self.config.views if v in model_views_all
        }
        total_model_edges = sum(v.edge_count for v in model_views.values())
        logger.info("  Extracted %d model edges in %.2fs",
                     total_model_edges, time.time() - t)

        # ── Step 5: Parse DXF ─────────────────────────────────────
        logger.info("── Step 5/8: Parsing 2D drawings ──")
        t = time.time()
        
        drawing_views = {}
        for view_name, file_path in drawing_file.items():
            logger.info("  Parsing drawing for view %s: %s", view_name.value, file_path)
            dxf_parser = DXFParser(file_path)
            views = dxf_parser.parse(view_name)
            if view_name in views:
                drawing_views[view_name] = views[view_name]
            
        total_drawing_edges = sum(v.edge_count for v in drawing_views.values())
        logger.info("  Extracted %d drawing edges in %.2fs",
                     total_drawing_edges, time.time() - t)

        self._apply_drawing_view_rotations(drawing_views)

        # ── Step 6: Normalize (geometry-based) ─────────────────────
        logger.info("── Step 6/9: Scaling drawings and preserving orientation ──")
        t = time.time()
        normalizer = Normalizer(drawing_scale=self.config.drawing_scale)
        norm_model = deepcopy(model_views)
        norm_drawing = normalizer.fit_all_drawings_to_models(
            model_views, deepcopy(drawing_views)
        )
        logger.info(
            "  Applied drawing scale %.6f without rotation/mirroring in %.2fs",
            self.config.drawing_scale,
            time.time() - t,
        )

        # ── Step 6.5: Image-based visual comparison ───────────────────
        logger.info("── Step 6.5/9: Image-based visual comparison ──")
        t = time.time()
        img_comparator = ImageComparator()
        image_results = img_comparator.compare_all_views(
            model_views, drawing_views
        )
        from pathlib import Path as _Path
        img_output = _Path(self.config.output_dir) / "image_comparison"
        img_output.mkdir(parents=True, exist_ok=True)
        img_comparator.save_all_debug_images(image_results, img_output)
        # Save the composite grid
        grid = img_comparator.create_comparison_grid(image_results)
        import cv2 as _cv2
        _cv2.imwrite(str(img_output / "comparison_grid.png"), grid)
        logger.info(
            "  Image comparison completed in %.2fs — %d views processed",
            time.time() - t, len(image_results),
        )
        for vn, res in image_results.items():
            logger.info(
                "    %s: overlap=%.1f%% model_cov=%.1f%% drawing_cov=%.1f%%",
                vn.value, res.overlap_score * 100,
                res.model_coverage * 100, res.drawing_coverage * 100,
            )

        # ── Step 7: Match ─────────────────────────────────────────
        logger.info("── Step 7/9: Matching edges ──")
        t = time.time()
        matcher = Matcher(
            distance_threshold=self.config.match_distance_threshold,
            bbox_expansion=self.config.match_bbox_expansion,
        )
        match_results = matcher.match_all_views(norm_model, norm_drawing)
        logger.info("  Matching completed in %.2fs", time.time() - t)

        # ── Step 8: Diff detection ────────────────────────────────
        logger.info("── Step 8/9: Detecting differences ──")
        t = time.time()
        detector = DiffDetector(
            modification_threshold=self.config.modification_threshold,
            min_edge_length=self.config.min_edge_length,
        )
        diff_results = detector.detect_all(match_results)
        logger.info("  Diff detection completed in %.2fs", time.time() - t)

        # ── Build report ──────────────────────────────────────────
        report = ValidationReport(
            step_file=str(step_file),
            dxf_file="Multiple files",
            view_results=diff_results,
        )

        # ── Step 9/9: Generate outputs ─────────────────────────────
        logger.info("── Step 9/9: Generating report ──")
        reporter = Reporter(output_dir=self.config.output_dir)
        reporter.generate(
            report,
            model_views=model_views_all,
            drawing_views=drawing_views,
            comparison_drawing_views=norm_drawing,
            image_comparison_results=image_results,
        )

        elapsed = time.time() - t_start
        logger.info("=" * 60)
        logger.info("PIPELINE COMPLETE in %.2fs", elapsed)
        logger.info("Overall Score: %.1f%%", report.overall_score * 100)
        logger.info("Status: %s", "PASSED" if report.passed else "FAILED")
        logger.info("Report: %s", self.config.output_dir)
        logger.info("=" * 60)

        return report

    def _apply_drawing_view_rotations(
        self,
        drawing_views: Dict[ViewName, "ViewGeometry"],
    ) -> None:
        """Rotate selected drawing views around their own center before comparison."""
        import math
        import numpy as np

        for view_name, degrees in self.config.drawing_view_rotations.items():
            if not degrees or view_name not in drawing_views:
                continue

            view_geom = drawing_views[view_name]
            point_sets = [edge.points for edge in view_geom.edges if len(edge.points)]
            if not point_sets:
                continue

            all_points = np.vstack(point_sets)
            center = all_points.mean(axis=0)
            radians = math.radians(float(degrees))
            c, s = math.cos(radians), math.sin(radians)
            matrix = np.array([[c, -s], [s, c]], dtype=np.float64)

            for edge in view_geom.edges:
                if len(edge.points):
                    edge.points = (edge.points - center) @ matrix.T + center
                    if len(edge.points) > 1:
                        diffs = np.diff(edge.points, axis=0)
                        edge.length = float(np.sum(np.linalg.norm(diffs, axis=1)))
                    edge.params["_gui_rotation_degrees"] = float(degrees)

            logger.info(
                "Applied GUI rotation %.1f degrees to %s drawing view",
                degrees,
                view_name.value,
            )
