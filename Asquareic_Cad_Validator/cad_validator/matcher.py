"""
Matcher
=======

Entity-level geometric matching between model and drawing edges.
Uses spatial indexing (R-tree via Shapely) and directed Hausdorff distance
for pairing edges across the two sources.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import numpy as np
from scipy.spatial.distance import directed_hausdorff
from shapely.geometry import LineString, box
from shapely.strtree import STRtree

from .geometry_store import Edge2D, MatchPair, ViewGeometry, ViewName

logger = logging.getLogger(__name__)


class Matcher:
    """
    Matches edges from model projections to DXF drawing edges.

    Algorithm:
        1. Build an R-tree spatial index on drawing edges (bounding boxes).
        2. For each model edge, query nearby drawing edges.
        3. Compute symmetric Hausdorff distance for each candidate pair.
        4. Use a greedy assignment: best-distance first, one-to-one.
    """

    def __init__(
        self,
        distance_threshold: float = 0.05,
        bbox_expansion: float = 0.1,
    ):
        """
        Args:
            distance_threshold:
                Maximum Hausdorff distance (in normalized units) to
                consider two edges as a match.
            bbox_expansion:
                How much to expand bounding boxes when querying the R-tree
                (fraction of the bbox diagonal).
        """
        self.distance_threshold = distance_threshold
        self.bbox_expansion = bbox_expansion

    def match_view(
        self,
        model_geom: ViewGeometry,
        drawing_geom: ViewGeometry,
    ) -> Tuple[List[MatchPair], List[Edge2D], List[Edge2D]]:
        """
        Match edges between model and drawing for a single view.

        Returns:
            (matched_pairs, unmatched_model_edges, unmatched_drawing_edges)
        """
        if not model_geom.edges or not drawing_geom.edges:
            return [], list(model_geom.edges), list(drawing_geom.edges)

        # Build spatial index on drawing edges
        drawing_geoms = []
        drawing_edge_map = {}
        for i, edge in enumerate(drawing_geom.edges):
            if len(edge.points) >= 2:
                ls = LineString(edge.points)
                drawing_geoms.append(ls)
                drawing_edge_map[id(ls)] = i

        if not drawing_geoms:
            return [], list(model_geom.edges), list(drawing_geom.edges)

        tree = STRtree(drawing_geoms)

        # For each model edge, find candidate drawing edges and compute distance
        candidates: List[Tuple[int, int, float]] = []  # (model_idx, drawing_idx, dist)

        for m_idx, m_edge in enumerate(model_geom.edges):
            if len(m_edge.points) < 2:
                continue

            m_ls = LineString(m_edge.points)
            m_bounds = m_ls.bounds  # (minx, miny, maxx, maxy)

            # Expand query box
            dx = (m_bounds[2] - m_bounds[0]) * self.bbox_expansion + self.bbox_expansion
            dy = (m_bounds[3] - m_bounds[1]) * self.bbox_expansion + self.bbox_expansion
            query_box = box(
                m_bounds[0] - dx, m_bounds[1] - dy,
                m_bounds[2] + dx, m_bounds[3] + dy,
            )

            nearby = tree.query(query_box)

            for d_geom in nearby:
                d_idx = drawing_edge_map.get(id(d_geom))
                if d_idx is None:
                    continue

                d_edge = drawing_geom.edges[d_idx]
                dist = self._hausdorff_distance(m_edge.points, d_edge.points)
                candidates.append((m_idx, d_idx, dist))

        # Greedy one-to-one assignment (best distance first)
        candidates.sort(key=lambda x: x[2])
        matched_model = set()
        matched_drawing = set()
        matched_pairs: List[MatchPair] = []

        for m_idx, d_idx, dist in candidates:
            if m_idx in matched_model or d_idx in matched_drawing:
                continue
            if dist > self.distance_threshold:
                continue

            confidence = max(0.0, 1.0 - dist / self.distance_threshold)
            matched_pairs.append(MatchPair(
                model_edge=model_geom.edges[m_idx],
                drawing_edge=drawing_geom.edges[d_idx],
                distance=dist,
                confidence=confidence,
            ))
            matched_model.add(m_idx)
            matched_drawing.add(d_idx)

        unmatched_model = [
            e for i, e in enumerate(model_geom.edges)
            if i not in matched_model
        ]
        unmatched_drawing = [
            e for i, e in enumerate(drawing_geom.edges)
            if i not in matched_drawing
        ]

        logger.info(
            "View %s: %d matched, %d model-only, %d drawing-only",
            model_geom.view_name.value,
            len(matched_pairs),
            len(unmatched_model),
            len(unmatched_drawing),
        )

        return matched_pairs, unmatched_model, unmatched_drawing

    def match_all_views(
        self,
        model_views: Dict[ViewName, ViewGeometry],
        drawing_views: Dict[ViewName, ViewGeometry],
    ) -> Dict[ViewName, Tuple[List[MatchPair], List[Edge2D], List[Edge2D]]]:
        """Match edges for all common views."""
        results = {}
        for view_name in model_views:
            if view_name in drawing_views:
                results[view_name] = self.match_view(
                    model_views[view_name],
                    drawing_views[view_name],
                )
            else:
                results[view_name] = (
                    [],
                    list(model_views[view_name].edges),
                    [],
                )
        # Drawing views with no model counterpart
        for view_name in drawing_views:
            if view_name not in model_views:
                results[view_name] = (
                    [],
                    [],
                    list(drawing_views[view_name].edges),
                )
        return results

    @staticmethod
    def _hausdorff_distance(pts_a: np.ndarray, pts_b: np.ndarray) -> float:
        """Symmetric Hausdorff distance between two point sets."""
        d1 = directed_hausdorff(pts_a, pts_b)[0]
        d2 = directed_hausdorff(pts_b, pts_a)[0]
        return max(d1, d2)
