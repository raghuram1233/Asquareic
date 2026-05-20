"""
Diff Detector
=============

Classifies matching results into structured DiffResult objects.
Applies additional heuristics to distinguish between truly missing
edges and edges that are merely modified (shifted/scaled slightly).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import numpy as np
from scipy.spatial.distance import directed_hausdorff

from .geometry_store import (
    DiffResult,
    Edge2D,
    MatchPair,
    ViewName,
)

logger = logging.getLogger(__name__)


class DiffDetector:
    """
    Takes raw matching results and produces classified DiffResult objects.

    Performs a second-pass analysis on unmatched edges to find
    near-misses (modified edges that didn't meet the strict threshold).
    """

    def __init__(
        self,
        modification_threshold: float = 0.15,
        min_edge_length: float = 0.005,
    ):
        """
        Args:
            modification_threshold:
                Hausdorff distance threshold (normalized) below which
                an unmatched pair is classified as MODIFIED rather than
                MISSING+EXTRA.
            min_edge_length:
                Edges shorter than this (normalized) are filtered out
                as noise / construction artifacts.
        """
        self.modification_threshold = modification_threshold
        self.min_edge_length = min_edge_length

    def detect(
        self,
        view_name: ViewName,
        matched_pairs: List[MatchPair],
        unmatched_model: List[Edge2D],
        unmatched_drawing: List[Edge2D],
    ) -> DiffResult:
        """
        Classify differences for a single view.

        Args:
            view_name:          The view being analyzed.
            matched_pairs:      Edges successfully paired by the Matcher.
            unmatched_model:    Model edges with no drawing counterpart.
            unmatched_drawing:  Drawing edges with no model counterpart.

        Returns:
            A DiffResult with edges classified into matched/missing/extra/modified.
        """
        result = DiffResult(view_name=view_name)

        # Filter noise edges
        unmatched_model = [
            e for e in unmatched_model if e.length >= self.min_edge_length
        ]
        unmatched_drawing = [
            e for e in unmatched_drawing if e.length >= self.min_edge_length
        ]

        # Classify strict matches
        for pair in matched_pairs:
            if pair.distance < self.modification_threshold * 0.5:
                result.matched.append(pair)
            else:
                result.modified.append(pair)

        # Second-pass: try to pair remaining unmatched edges as modifications
        remaining_model, remaining_drawing = self._second_pass_matching(
            unmatched_model, unmatched_drawing, result
        )

        # Whatever is left is truly missing or extra
        result.missing_in_drawing.extend(remaining_model)
        result.extra_in_drawing.extend(remaining_drawing)

        logger.info(
            "Diff %s: matched=%d, modified=%d, missing=%d, extra=%d, score=%.4f",
            view_name.value,
            len(result.matched),
            len(result.modified),
            len(result.missing_in_drawing),
            len(result.extra_in_drawing),
            result.match_score,
        )

        return result

    def _second_pass_matching(
        self,
        model_edges: List[Edge2D],
        drawing_edges: List[Edge2D],
        result: DiffResult,
    ) -> Tuple[List[Edge2D], List[Edge2D]]:
        """
        Try to pair leftover edges as modifications using a relaxed threshold.

        Returns remaining truly-unmatched edges from both sides.
        """
        if not model_edges or not drawing_edges:
            return model_edges, drawing_edges

        # Compute pairwise Hausdorff distances
        n_m = len(model_edges)
        n_d = len(drawing_edges)
        dist_matrix = np.full((n_m, n_d), np.inf)

        for i, me in enumerate(model_edges):
            for j, de in enumerate(drawing_edges):
                if len(me.points) >= 2 and len(de.points) >= 2:
                    d1 = directed_hausdorff(me.points, de.points)[0]
                    d2 = directed_hausdorff(de.points, me.points)[0]
                    dist_matrix[i, j] = max(d1, d2)

        # Greedy assignment within modification threshold
        used_m = set()
        used_d = set()

        while True:
            # Find minimum distance
            if dist_matrix.size == 0:
                break
            min_val = np.min(dist_matrix)
            if min_val > self.modification_threshold:
                break

            min_idx = np.unravel_index(np.argmin(dist_matrix), dist_matrix.shape)
            i, j = int(min_idx[0]), int(min_idx[1])

            if i in used_m or j in used_d:
                dist_matrix[i, j] = np.inf
                continue

            confidence = max(0.0, 1.0 - min_val / self.modification_threshold)
            result.modified.append(MatchPair(
                model_edge=model_edges[i],
                drawing_edge=drawing_edges[j],
                distance=float(min_val),
                confidence=confidence,
            ))

            used_m.add(i)
            used_d.add(j)
            dist_matrix[i, :] = np.inf
            dist_matrix[:, j] = np.inf

        remaining_model = [e for i, e in enumerate(model_edges) if i not in used_m]
        remaining_drawing = [e for j, e in enumerate(drawing_edges) if j not in used_d]

        return remaining_model, remaining_drawing

    def detect_all(
        self,
        match_results: Dict[ViewName, Tuple[List[MatchPair], List[Edge2D], List[Edge2D]]],
    ) -> Dict[ViewName, DiffResult]:
        """Run diff detection for all views."""
        return {
            view_name: self.detect(view_name, *match_data)
            for view_name, match_data in match_results.items()
        }
