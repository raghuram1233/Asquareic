"""
Reporter
========

Generates visual overlays (matplotlib) and structured JSON reports
from the validation results.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

from .geometry_store import DiffResult, ValidationReport, ViewGeometry, ViewName

if TYPE_CHECKING:
    from .image_comparator import OverlayResult

logger = logging.getLogger(__name__)

# Color scheme for the report overlays
COLORS = {
    "matched_model": "#2ecc71",      # green
    "matched_drawing": "#27ae60",    # darker green
    "missing": "#e74c3c",            # red — in model but not drawing
    "extra": "#3498db",              # blue — in drawing but not model
    "modified_model": "#f39c12",     # orange
    "modified_drawing": "#e67e22",   # darker orange
    "model": "#f1c40f",               # yellow - model-only projection
    "drawing": "#5dade2",             # blue - drawing-only extraction
    "background": "#1a1a2e",
    "grid": "#2a2a4a",
}


class Reporter:
    """
    Generates per-view visual overlays and a summary JSON report.
    """

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        report: ValidationReport,
        model_views: Optional[Dict[ViewName, ViewGeometry]] = None,
        drawing_views: Optional[Dict[ViewName, ViewGeometry]] = None,
        comparison_drawing_views: Optional[Dict[ViewName, ViewGeometry]] = None,
        image_comparison_results: Optional[Dict[ViewName, "OverlayResult"]] = None,
    ) -> Path:
        """
        Generate the full report: model views, per-view PNGs, and summary JSON.

        Returns:
            Path to the output directory.
        """
        logger.info("Generating validation report in: %s", self.output_dir)
        model_output_paths = {}
        if model_views:
            model_output_paths = self._render_model_views(model_views)

        drawing_output_paths = {}
        if drawing_views:
            drawing_output_paths = self._render_drawing_views(drawing_views)

        # Generate summary image (grid of all views)
        self._render_summary_grid(report, comparison_drawing_views or {})
        if model_views or drawing_views:
            self._render_all_views_grid(
                report,
                model_views or {},
                drawing_views or {},
                comparison_drawing_views or {},
            )

        # Generate image-based comparison outputs
        if image_comparison_results:
            self._render_image_comparison_grid(image_comparison_results)

        # Generate JSON report
        json_path = self.output_dir / "validation_report.json"
        report_data = {
            "generated_at": datetime.now().isoformat(),
            "step_file": report.step_file,
            "dxf_file": report.dxf_file,
            "overall_score": round(report.overall_score, 4),
            "passed": report.passed,
            "model_outputs": {
                view.value: str(path)
                for view, path in model_output_paths.items()
            },
            "model_outputs_grid": (
                str(self.output_dir / "model_views_grid.png")
                if model_output_paths else None
            ),
            "drawing_outputs": {
                view.value: str(path)
                for view, path in drawing_output_paths.items()
            },
            "drawing_outputs_grid": (
                str(self.output_dir / "drawing_views_grid.png")
                if drawing_output_paths else None
            ),
            "all_views_grid": (
                str(self.output_dir / "all_views_grid.png")
                if model_views or drawing_views else None
            ),
            "image_comparison_grid": (
                str(self.output_dir / "image_comparison_grid.png")
                if image_comparison_results else None
            ),
            "views": {},
        }

        for view_name, diff in report.view_results.items():
            view_data = diff.summary()
            view_data["details"] = {
                "missing_edges": [
                    {"type": e.edge_type.value, "length": round(e.length, 6)}
                    for e in diff.missing_in_drawing
                ],
                "extra_edges": [
                    {"type": e.edge_type.value, "length": round(e.length, 6)}
                    for e in diff.extra_in_drawing
                ],
                "modified_edges": [
                    {
                        "distance": round(m.distance, 6),
                        "confidence": round(m.confidence, 4),
                        "model_type": m.model_edge.edge_type.value,
                        "drawing_type": m.drawing_edge.edge_type.value,
                    }
                    for m in diff.modified
                ],
            }
            # Add image-based comparison scores if available
            if image_comparison_results and view_name in image_comparison_results:
                img_res = image_comparison_results[view_name]
                view_data["image_comparison"] = {
                    "overlap_score": round(img_res.overlap_score, 4),
                    "model_coverage": round(img_res.model_coverage, 4),
                    "drawing_coverage": round(img_res.drawing_coverage, 4),
                }
            report_data["views"][view_name.value] = view_data

        # Add image comparison summary to top level
        if image_comparison_results:
            img_scores = [
                r.overlap_score for r in image_comparison_results.values()
            ]
            report_data["image_comparison_overall"] = round(
                sum(img_scores) / max(len(img_scores), 1), 4
            )

        with open(json_path, "w") as f:
            json.dump(report_data, f, indent=2)

        logger.info("JSON report saved: %s", json_path)

        # Generate text summary
        self._write_text_summary(report)

        return self.output_dir

    def _render_view_overlay(
        self,
        view_name: ViewName,
        diff: DiffResult,
        drawing_underlay: Optional[ViewGeometry] = None,
    ):
        """Render a single view's overlay showing matches, diffs."""
        fig, ax = plt.subplots(1, 1, figsize=(12, 10))
        ax.set_facecolor(COLORS["background"])
        fig.patch.set_facecolor(COLORS["background"])

        if drawing_underlay is not None:
            for edge in drawing_underlay.edges:
                self._plot_edge(ax, edge.points, COLORS["drawing"],
                                alpha=0.28, linewidth=0.8)

        # Draw matched edges
        for pair in diff.matched:
            self._plot_edge(ax, pair.model_edge.points, COLORS["matched_model"],
                            alpha=0.6, linewidth=1.0)

        # Draw modified edges (both versions)
        for pair in diff.modified:
            self._plot_edge(ax, pair.model_edge.points, COLORS["modified_model"],
                            alpha=0.8, linewidth=1.5, linestyle="--")
            self._plot_edge(ax, pair.drawing_edge.points, COLORS["modified_drawing"],
                            alpha=0.8, linewidth=1.5, linestyle=":")

        # Draw missing edges (in model, not in drawing)
        for edge in diff.missing_in_drawing:
            self._plot_edge(ax, edge.points, COLORS["missing"],
                            alpha=0.9, linewidth=2.0)

        # Draw extra edges (in drawing, not in model)
        for edge in diff.extra_in_drawing:
            self._plot_edge(ax, edge.points, COLORS["extra"],
                            alpha=0.9, linewidth=2.0)

        # Legend
        legend_items = [
            mpatches.Patch(color=COLORS["matched_model"], label="Matched"),
            mpatches.Patch(color=COLORS["missing"], label="Missing in Drawing"),
            mpatches.Patch(color=COLORS["extra"], label="Extra in Drawing"),
            mpatches.Patch(color=COLORS["modified_model"], label="Modified (Model)"),
            mpatches.Patch(color=COLORS["modified_drawing"], label="Modified (Drawing)"),
        ]
        ax.legend(handles=legend_items, loc="upper right",
                  facecolor=COLORS["background"], edgecolor="#444",
                  labelcolor="white", fontsize=9)

        ax.set_title(
            f"{view_name.value.upper()} View — Score: {diff.match_score:.1%}",
            color="white", fontsize=14, fontweight="bold",
        )
        ax.set_aspect("equal")
        ax.grid(True, color=COLORS["grid"], alpha=0.3, linewidth=0.5)
        ax.tick_params(colors="white", labelsize=8)

        for spine in ax.spines.values():
            spine.set_color("#444")

        fig_path = self.output_dir / f"view_{view_name.value}.png"
        fig.savefig(fig_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info("Saved view overlay: %s", fig_path)

    def _render_summary_grid(
        self,
        report: ValidationReport,
        comparison_drawing_views: Optional[Dict[ViewName, ViewGeometry]] = None,
    ):
        """Render a 2x3 grid of all view overlays."""
        view_order = [
            ViewName.FRONT, ViewName.BACK,
            ViewName.TOP, ViewName.BOTTOM,
            ViewName.LEFT, ViewName.RIGHT,
        ]

        fig, axes = plt.subplots(3, 2, figsize=(20, 24))
        fig.patch.set_facecolor(COLORS["background"])

        for idx, view_name in enumerate(view_order):
            row, col = divmod(idx, 2)
            ax = axes[row][col]
            ax.set_facecolor(COLORS["background"])

            if view_name in report.view_results:
                diff = report.view_results[view_name]
                drawing_underlay = (comparison_drawing_views or {}).get(view_name)

                if drawing_underlay is not None:
                    for edge in drawing_underlay.edges:
                        self._plot_edge(ax, edge.points, COLORS["drawing"],
                                        alpha=0.25, linewidth=0.6)

                for pair in diff.matched:
                    self._plot_edge(ax, pair.model_edge.points,
                                    COLORS["matched_model"], alpha=0.5, linewidth=0.8)
                for pair in diff.modified:
                    self._plot_edge(ax, pair.model_edge.points,
                                    COLORS["modified_model"], alpha=0.7, linewidth=1.2)
                for edge in diff.missing_in_drawing:
                    self._plot_edge(ax, edge.points,
                                    COLORS["missing"], alpha=0.9, linewidth=1.5)
                for edge in diff.extra_in_drawing:
                    self._plot_edge(ax, edge.points,
                                    COLORS["extra"], alpha=0.9, linewidth=1.5)

                score = diff.match_score
                status = "PASS" if not diff.has_differences else "FAIL"
                color = "#2ecc71" if status == "PASS" else "#e74c3c"
                ax.set_title(
                    f"{view_name.value.upper()} — {score:.1%} [{status}]",
                    color=color, fontsize=12, fontweight="bold",
                )
            else:
                ax.text(0.5, 0.5, "NO DATA", transform=ax.transAxes,
                        ha="center", va="center", color="#666", fontsize=16)
                ax.set_title(f"{view_name.value.upper()} — N/A",
                             color="#666", fontsize=12)

            ax.set_aspect("equal")
            ax.grid(True, color=COLORS["grid"], alpha=0.2)
            ax.tick_params(colors="white", labelsize=6)
            for spine in ax.spines.values():
                spine.set_color("#333")

        overall = report.overall_score
        status = "PASSED" if report.passed else "FAILED"
        fig.suptitle(
            f"CAD Validation Summary — {status} ({overall:.1%})",
            color="white", fontsize=18, fontweight="bold", y=0.98,
        )

        fig.tight_layout(rect=[0, 0, 1, 0.96])
        summary_path = self.output_dir / "summary_grid.png"
        fig.savefig(summary_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info("Saved summary grid: %s", summary_path)

    def _write_text_summary(self, report: ValidationReport):
        """Write a plain-text summary report."""
        txt_path = self.output_dir / "validation_summary.txt"
        lines = [
            "=" * 60,
            "CAD VALIDATION REPORT",
            "=" * 60,
            f"Generated: {datetime.now().isoformat()}",
            f"STEP File: {report.step_file}",
            f"DXF File:  {report.dxf_file}",
            f"Overall Score: {report.overall_score:.1%}",
            f"Status: {'PASSED' if report.passed else 'FAILED'}",
            "-" * 60,
        ]

        for view_name, diff in report.view_results.items():
            s = diff.summary()
            status = "PASS" if not diff.has_differences else "FAIL"
            lines.append(f"\n[{view_name.value.upper()}] — {status}")
            lines.append(f"  Score:    {s['match_score']:.1%}")
            lines.append(f"  Matched:  {s['matched']}")
            lines.append(f"  Missing:  {s['missing_in_drawing']}")
            lines.append(f"  Extra:    {s['extra_in_drawing']}")
            lines.append(f"  Modified: {s['modified']}")

        lines.append("\n" + "=" * 60)

        with open(txt_path, "w") as f:
            f.write("\n".join(lines))

        logger.info("Saved text summary: %s", txt_path)

    def _render_model_views(
        self,
        model_views: Dict[ViewName, ViewGeometry],
    ) -> Dict[ViewName, Path]:
        """Render model-only PNGs for the six standard orthographic views."""
        output_paths: Dict[ViewName, Path] = {}
        for view_name in ViewName:
            view_geom = model_views.get(view_name)
            if view_geom is not None:
                output_paths[view_name] = self.output_dir / f"model_view_{view_name.value}.png"
                # Skipping individual view generation per user request

        self._render_model_views_grid(model_views)
        logger.info("Saved %d model-only view outputs", len(output_paths))
        return output_paths

    def _render_model_view(
        self,
        view_name: ViewName,
        view_geom: ViewGeometry,
    ) -> Path:
        """Render one model-only orthographic projection."""
        fig, ax = plt.subplots(1, 1, figsize=(12, 10))
        ax.set_facecolor(COLORS["background"])
        fig.patch.set_facecolor(COLORS["background"])

        for edge in view_geom.edges:
            self._plot_edge(ax, edge.points, COLORS["model"],
                            alpha=0.95, linewidth=1.0)

        ax.set_title(
            f"3D Model {view_name.value.upper()} View",
            color="white", fontsize=14, fontweight="bold",
        )
        self._style_axes(ax)

        fig_path = self.output_dir / f"model_view_{view_name.value}.png"
        fig.savefig(fig_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info("Saved model view: %s", fig_path)
        return fig_path

    def _render_model_views_grid(
        self,
        model_views: Dict[ViewName, ViewGeometry],
    ):
        """Render a 2x3 grid of model-only orthographic projections."""
        view_order = [
            ViewName.FRONT, ViewName.BACK,
            ViewName.TOP, ViewName.BOTTOM,
            ViewName.LEFT, ViewName.RIGHT,
        ]

        fig, axes = plt.subplots(3, 2, figsize=(20, 24))
        fig.patch.set_facecolor(COLORS["background"])

        for idx, view_name in enumerate(view_order):
            row, col = divmod(idx, 2)
            ax = axes[row][col]
            ax.set_facecolor(COLORS["background"])

            view_geom = model_views.get(view_name)
            if view_geom is not None:
                for edge in view_geom.edges:
                    self._plot_edge(ax, edge.points, COLORS["model"],
                                    alpha=0.95, linewidth=0.8)
                ax.set_title(
                    f"{view_name.value.upper()} - {view_geom.edge_count} edges",
                    color="white", fontsize=12, fontweight="bold",
                )
            else:
                ax.text(0.5, 0.5, "NO DATA", transform=ax.transAxes,
                        ha="center", va="center", color="#666", fontsize=16)
                ax.set_title(f"{view_name.value.upper()} - N/A",
                             color="#666", fontsize=12)

            self._style_axes(ax, tick_label_size=6)

        fig.suptitle(
            "3D Model Orthographic Views",
            color="white", fontsize=18, fontweight="bold", y=0.98,
        )

        fig.tight_layout(rect=[0, 0, 1, 0.96])
        summary_path = self.output_dir / "model_views_grid.png"
        fig.savefig(summary_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info("Saved model views grid: %s", summary_path)

    def _render_drawing_views(
        self,
        drawing_views: Dict[ViewName, ViewGeometry],
    ) -> Dict[ViewName, Path]:
        """Render drawing-only PNGs for the extracted 2D views."""
        output_paths: Dict[ViewName, Path] = {}
        for view_name in ViewName:
            view_geom = drawing_views.get(view_name)
            if view_geom is not None:
                output_paths[view_name] = self.output_dir / f"drawing_view_{view_name.value}.png"
                # Skipping individual view generation per user request

        self._render_drawing_views_grid(drawing_views)
        logger.info("Saved %d drawing-only view outputs", len(output_paths))
        return output_paths

    def _render_drawing_view(
        self,
        view_name: ViewName,
        view_geom: ViewGeometry,
    ) -> Path:
        """Render one drawing-only extracted 2D view."""
        fig, ax = plt.subplots(1, 1, figsize=(12, 10))
        ax.set_facecolor(COLORS["background"])
        fig.patch.set_facecolor(COLORS["background"])

        for edge in view_geom.edges:
            self._plot_edge(ax, edge.points, COLORS["drawing"],
                            alpha=0.95, linewidth=1.0)

        ax.set_title(
            f"2D Drawing {view_name.value.upper()} View",
            color="white", fontsize=14, fontweight="bold",
        )
        self._style_axes(ax)
        self._crop_to_view_bounds(ax, view_geom)

        fig_path = self.output_dir / f"drawing_view_{view_name.value}.png"
        fig.savefig(fig_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info("Saved drawing view: %s", fig_path)
        return fig_path

    def _render_drawing_views_grid(
        self,
        drawing_views: Dict[ViewName, ViewGeometry],
    ):
        """Render a 2x3 grid of extracted 2D drawing views."""
        view_order = [
            ViewName.FRONT, ViewName.BACK,
            ViewName.TOP, ViewName.BOTTOM,
            ViewName.LEFT, ViewName.RIGHT,
        ]

        fig, axes = plt.subplots(3, 2, figsize=(20, 24))
        fig.patch.set_facecolor(COLORS["background"])

        for idx, view_name in enumerate(view_order):
            row, col = divmod(idx, 2)
            ax = axes[row][col]
            ax.set_facecolor(COLORS["background"])

            view_geom = drawing_views.get(view_name)
            if view_geom is not None:
                for edge in view_geom.edges:
                    self._plot_edge(ax, edge.points, COLORS["drawing"],
                                    alpha=0.95, linewidth=0.8)
                ax.set_title(
                    f"{view_name.value.upper()} - {view_geom.edge_count} edges",
                    color="white", fontsize=12, fontweight="bold",
                )
            else:
                ax.text(0.5, 0.5, "NO DATA", transform=ax.transAxes,
                        ha="center", va="center", color="#666", fontsize=16)
                ax.set_title(f"{view_name.value.upper()} - N/A",
                             color="#666", fontsize=12)

            self._style_axes(ax, tick_label_size=6)
            if view_geom is not None:
                self._crop_to_view_bounds(ax, view_geom)

        fig.suptitle(
            "2D Drawing Extracted Views",
            color="white", fontsize=18, fontweight="bold", y=0.98,
        )

        fig.tight_layout(rect=[0, 0, 1, 0.96])
        summary_path = self.output_dir / "drawing_views_grid.png"
        fig.savefig(summary_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info("Saved drawing views grid: %s", summary_path)

    def _render_all_views_grid(
        self,
        report: ValidationReport,
        model_views: Dict[ViewName, ViewGeometry],
        drawing_views: Dict[ViewName, ViewGeometry],
        comparison_drawing_views: Dict[ViewName, ViewGeometry],
    ):
        """Render one image containing model, drawing, and comparison views."""
        view_order = [
            ViewName.FRONT, ViewName.BACK,
            ViewName.TOP, ViewName.BOTTOM,
            ViewName.LEFT, ViewName.RIGHT,
        ]
        columns = [
            ("3D Model", "model"),
            ("2D Drawing", "drawing"),
            ("Comparison", "comparison"),
        ]

        fig, axes = plt.subplots(len(view_order), len(columns), figsize=(24, 36))
        fig.patch.set_facecolor(COLORS["background"])

        for row, view_name in enumerate(view_order):
            for col, (column_title, column_kind) in enumerate(columns):
                ax = axes[row][col]
                ax.set_facecolor(COLORS["background"])

                if column_kind == "model":
                    view_geom = model_views.get(view_name)
                    if view_geom is not None:
                        for edge in view_geom.edges:
                            self._plot_edge(ax, edge.points, COLORS["model"],
                                            alpha=0.95, linewidth=0.7)
                        subtitle = f"{view_geom.edge_count} edges"
                    else:
                        subtitle = None

                elif column_kind == "drawing":
                    view_geom = drawing_views.get(view_name)
                    if view_geom is not None:
                        for edge in view_geom.edges:
                            self._plot_edge(ax, edge.points, COLORS["drawing"],
                                            alpha=0.95, linewidth=0.7)
                        subtitle = f"{view_geom.edge_count} edges"
                    else:
                        subtitle = None

                else:
                    diff = report.view_results.get(view_name)
                    if diff is not None:
                        drawing_underlay = comparison_drawing_views.get(view_name)
                        if drawing_underlay is not None:
                            for edge in drawing_underlay.edges:
                                self._plot_edge(ax, edge.points, COLORS["drawing"],
                                                alpha=0.25, linewidth=0.5)

                        for pair in diff.matched:
                            self._plot_edge(ax, pair.model_edge.points,
                                            COLORS["matched_model"], alpha=0.5,
                                            linewidth=0.7)
                        for pair in diff.modified:
                            self._plot_edge(ax, pair.model_edge.points,
                                            COLORS["modified_model"], alpha=0.7,
                                            linewidth=1.0)
                        for edge in diff.missing_in_drawing:
                            self._plot_edge(ax, edge.points, COLORS["missing"],
                                            alpha=0.9, linewidth=1.1)
                        for edge in diff.extra_in_drawing:
                            self._plot_edge(ax, edge.points, COLORS["extra"],
                                            alpha=0.9, linewidth=1.1)
                        subtitle = f"{diff.match_score:.1%}"
                    else:
                        subtitle = None

                if subtitle is None:
                    ax.text(0.5, 0.5, "NO DATA", transform=ax.transAxes,
                            ha="center", va="center", color="#666", fontsize=13)
                    subtitle = "N/A"

                ax.set_title(
                    f"{view_name.value.upper()} - {column_title} ({subtitle})",
                    color="white", fontsize=10, fontweight="bold",
                )
                self._style_axes(ax, tick_label_size=5)
                if column_kind == "drawing" and view_geom is not None:
                    self._crop_to_view_bounds(ax, view_geom)

        fig.suptitle(
            "All Extracted Views",
            color="white", fontsize=20, fontweight="bold", y=0.995,
        )

        fig.tight_layout(rect=[0, 0, 1, 0.985])
        summary_path = self.output_dir / "all_views_grid.png"
        fig.savefig(summary_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info("Saved all views grid: %s", summary_path)

    def _render_image_comparison_grid(
        self,
        image_results: Dict[ViewName, "OverlayResult"],
    ):
        """
        Render a matplotlib grid showing the image-based comparison results.

        Layout: 6 rows (views) × 4 columns (Model | Drawing | Overlay | Diff)
        """
        view_order = [
            ViewName.FRONT, ViewName.BACK,
            ViewName.TOP, ViewName.BOTTOM,
            ViewName.LEFT, ViewName.RIGHT,
        ]

        col_titles = ["3D Model (cropped)", "2D Drawing (cropped)", "Overlay", "Diff"]
        n_cols = 4

        fig, axes = plt.subplots(len(view_order), n_cols, figsize=(28, 36))
        fig.patch.set_facecolor(COLORS["background"])

        for row, view_name in enumerate(view_order):
            res = image_results.get(view_name)

            for col in range(n_cols):
                ax = axes[row][col]
                ax.set_facecolor(COLORS["background"])
                ax.set_xticks([])
                ax.set_yticks([])
                for spine in ax.spines.values():
                    spine.set_color("#333")

                if res is None:
                    ax.text(0.5, 0.5, "NO DATA", transform=ax.transAxes,
                            ha="center", va="center", color="#666", fontsize=14)
                    ax.set_title(
                        f"{view_name.value.upper()} — {col_titles[col]}",
                        color="#666", fontsize=10,
                    )
                    continue

                # Select the appropriate image
                if col == 0:
                    img = res.model_image
                elif col == 1:
                    img = res.drawing_image
                elif col == 2:
                    img = res.overlay_image
                else:
                    img = res.diff_image

                # Convert BGR → RGB for matplotlib display
                img_rgb = img[:, :, ::-1] if img is not None else None

                if img_rgb is not None:
                    ax.imshow(img_rgb, aspect="equal")
                else:
                    ax.text(0.5, 0.5, "EMPTY", transform=ax.transAxes,
                            ha="center", va="center", color="#666", fontsize=14)

                # Title with score for overlay/diff columns
                if col >= 2 and res is not None:
                    score_str = f" [{res.overlap_score:.1%}]"
                    color = "#2ecc71" if res.overlap_score > 0.5 else "#e74c3c"
                else:
                    score_str = ""
                    color = "white"

                ax.set_title(
                    f"{view_name.value.upper()} — {col_titles[col]}{score_str}",
                    color=color, fontsize=10, fontweight="bold",
                )

        # Overall score in title
        all_scores = [r.overlap_score for r in image_results.values()]
        avg_score = sum(all_scores) / max(len(all_scores), 1)
        fig.suptitle(
            f"Image-Based Visual Comparison — Avg Overlap: {avg_score:.1%}",
            color="white", fontsize=20, fontweight="bold", y=0.995,
        )

        fig.tight_layout(rect=[0, 0, 1, 0.985])
        grid_path = self.output_dir / "image_comparison_grid.png"
        fig.savefig(grid_path, dpi=150, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info("Saved image comparison grid: %s", grid_path)

    def _crop_to_view_bounds(
        self,
        ax,
        view_geom: ViewGeometry,
    ):
        """
        Crop the axes to the dominant view bounds without changing geometry.

        Off-view DXF fragments may still exist in the parsed data, but they no
        longer control the rendered scale or placement of the extracted view.
        """
        bounds = self._dominant_cluster_bounds(view_geom)
        if bounds is None:
            return

        min_x, min_y, max_x, max_y = bounds
        width = max(max_x - min_x, 1e-9)
        height = max(max_y - min_y, 1e-9)
        pad = max(width, height) * 0.01

        ax.set_xlim(min_x - pad, max_x + pad)
        ax.set_ylim(min_y - pad, max_y + pad)

    def _dominant_cluster_bounds(
        self,
        view_geom: ViewGeometry,
        distance_factor: float = 0.18,
    ) -> Optional[Tuple[float, float, float, float]]:
        """Find bounds of the dominant spatial cluster without editing edges."""
        if not view_geom.edges:
            return None

        if len(view_geom.edges) < 3:
            return view_geom.bounding_box

        centroids = np.array([edge.centroid for edge in view_geom.edges])
        extents = centroids.max(axis=0) - centroids.min(axis=0)
        threshold = max(float(max(extents)), 1.0) * distance_factor

        parent = list(range(len(view_geom.edges)))

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
        for i in range(len(view_geom.edges)):
            clusters.setdefault(find(i), []).append(i)

        def cluster_weight(indices: List[int]) -> float:
            return sum(view_geom.edges[i].length for i in indices)

        main_indices = max(clusters.values(), key=cluster_weight)
        points = np.vstack([
            view_geom.edges[i].points
            for i in main_indices
            if len(view_geom.edges[i].points)
        ])
        mins = points.min(axis=0)
        maxs = points.max(axis=0)
        return (float(mins[0]), float(mins[1]), float(maxs[0]), float(maxs[1]))

    @staticmethod
    def _style_axes(ax, tick_label_size: int = 8):
        """Apply shared report styling to a matplotlib axes."""
        ax.set_aspect("equal")
        ax.grid(True, color=COLORS["grid"], alpha=0.3, linewidth=0.5)
        ax.tick_params(colors="white", labelsize=tick_label_size)

        for spine in ax.spines.values():
            spine.set_color("#444")

    @staticmethod
    def _plot_edge(ax, points, color, alpha=1.0, linewidth=1.0, linestyle="-"):
        """Plot an edge on a matplotlib axes."""
        if len(points) < 2:
            return
        ax.plot(points[:, 0], points[:, 1],
                color=color, alpha=alpha,
                linewidth=linewidth, linestyle=linestyle)
