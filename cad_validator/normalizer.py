"""
Normalizer
==========

Normalizes 2D geometry from both sources (model projection and DXF drawing)
into a common coordinate system for comparison using Scale-Invariant ICP.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Dict, Optional, Tuple

import numpy as np
from scipy.spatial import cKDTree

from .geometry_store import Edge2D, ViewGeometry, ViewName

logger = logging.getLogger(__name__)

class Normalizer:
    """
    Fits ViewGeometry instances so that DXF drawings match model projections
    in size and orientation using Scale-Invariant ICP.
    """

    def fit_drawing_to_model(
        self,
        model_geom: ViewGeometry,
        drawing_geom: ViewGeometry,
    ) -> ViewGeometry:
        """Align, scale, and translate the drawing geometry to match the model geometry."""
        if not model_geom.edges or not drawing_geom.edges:
            return deepcopy(drawing_geom)

        m_pts = self._sample_points(self._all_points(model_geom))
        d_pts = self._sample_points(self._all_points(drawing_geom))

        if len(m_pts) < 2 or len(d_pts) < 2:
            return deepcopy(drawing_geom)

        # Center model points at origin for ICP
        m_centroid = m_pts.mean(axis=0)
        m_pts_centered = m_pts - m_centroid

        best_score = float("inf")
        best_scale = 1.0
        best_translation = np.zeros(2)
        best_matrix = np.eye(2)
        best_rotation = 0
        best_mirror_x = False
        best_mirror_y = False

        for rotation_degrees in (0, 90, 180, 270):
            rotation = self._rotation_matrix(rotation_degrees)
            for mirror_x in (False, True):
                for mirror_y in (False, True):
                    mirror = np.array([
                        [-1.0 if mirror_x else 1.0, 0.0],
                        [0.0, -1.0 if mirror_y else 1.0],
                    ])
                    matrix = rotation @ mirror
                    
                    oriented_d_pts = d_pts @ matrix.T
                    scale, translation, score = self._scale_translation_icp(m_pts_centered, oriented_d_pts)
                    
                    if score < best_score:
                        best_score = score
                        best_scale = scale
                        best_translation = translation
                        best_matrix = matrix
                        best_rotation = rotation_degrees
                        best_mirror_x = mirror_x
                        best_mirror_y = mirror_y

        result = deepcopy(drawing_geom)
        for edge in result.edges:
            pts = edge.points @ best_matrix.T
            pts = (pts * best_scale) + best_translation
            edge.points = pts + m_centroid
            
            if len(edge.points) > 1:
                diffs = np.diff(edge.points, axis=0)
                edge.length = float(np.sum(np.linalg.norm(diffs, axis=1)))
                
            edge.params["_alignment"] = {
                "rotation_degrees": best_rotation,
                "mirror_x": best_mirror_x,
                "mirror_y": best_mirror_y,
                "scale": round(best_scale, 6),
                "score": round(best_score, 6),
            }

        logger.info(
            "View %s alignment: rotate=%d mirror_x=%s mirror_y=%s scale=%.4f score=%.6f",
            drawing_geom.view_name.value,
            best_rotation,
            best_mirror_x,
            best_mirror_y,
            best_scale,
            best_score,
        )
        return result

    def fit_all_drawings_to_models(
        self,
        model_views: Dict[ViewName, ViewGeometry],
        drawing_views: Dict[ViewName, ViewGeometry],
    ) -> Dict[ViewName, ViewGeometry]:
        """Fit all drawing views to match their corresponding model views."""
        fitted = {}
        for vn, drawing_geom in drawing_views.items():
            if vn in model_views:
                fitted[vn] = self.fit_drawing_to_model(model_views[vn], drawing_geom)
            else:
                fitted[vn] = deepcopy(drawing_geom)
        return fitted

    def _scale_translation_icp(
        self, target_pts: np.ndarray, source_pts: np.ndarray, max_iters: int = 25, tol: float = 1e-4
    ) -> Tuple[float, np.ndarray, float]:
        """
        Iterative Closest Point to find optimal scale and translation.
        Assumes target_pts are centered at origin.
        """
        tree = cKDTree(target_pts)
        
        current_pts = source_pts.copy()
        current_scale = 1.0
        current_translation = np.zeros(2)
        prev_error = float("inf")
        
        # Initial guess based on bounding box
        t_bbox = target_pts.max(axis=0) - target_pts.min(axis=0)
        s_bbox = source_pts.max(axis=0) - source_pts.min(axis=0)
        t_max = max(t_bbox[0], t_bbox[1])
        s_max = max(s_bbox[0], s_bbox[1])
        
        if s_max > 1e-10:
            current_scale = t_max / s_max
            current_pts = current_pts * current_scale
            
        # Initial translation guess: align centroids
        t_centroid = target_pts.mean(axis=0)
        s_centroid = current_pts.mean(axis=0)
        current_translation = t_centroid - s_centroid
        current_pts = current_pts + current_translation

        for _ in range(max_iters):
            distances, indices = tree.query(current_pts)
            
            # Reject top 20% furthest points as outliers (annotations, borders)
            threshold = np.percentile(distances, 80)
            valid_mask = distances <= threshold
            
            if not np.any(valid_mask):
                break
                
            matched_target = target_pts[indices[valid_mask]]
            matched_source = source_pts[valid_mask] # Original unscaled
            
            src_mean = matched_source.mean(axis=0)
            tgt_mean = matched_target.mean(axis=0)
            src_demean = matched_source - src_mean
            tgt_demean = matched_target - tgt_mean
            
            variance = np.sum(src_demean**2)
            if variance < 1e-10:
                break
            scale = np.sum(src_demean * tgt_demean) / variance
            
            translation = tgt_mean - scale * src_mean
            current_pts = source_pts * scale + translation
            
            # Robust error: mean distance of the valid matches
            new_error = float(np.mean(np.linalg.norm(matched_source * scale + translation - matched_target, axis=1)))
            
            if abs(prev_error - new_error) < tol:
                current_scale = scale
                current_translation = translation
                prev_error = new_error
                break
                
            current_scale = scale
            current_translation = translation
            prev_error = new_error
            
        return current_scale, current_translation, prev_error

    @staticmethod
    def _all_points(view_geom: ViewGeometry) -> np.ndarray:
        """Return all edge points for a view."""
        point_sets = [e.points for e in view_geom.edges if len(e.points)]
        if not point_sets:
            return np.empty((0, 2), dtype=np.float64)
        return np.vstack(point_sets)

    @staticmethod
    def _sample_points(points: np.ndarray, max_points: int = 2000) -> np.ndarray:
        """Downsample points deterministically for faster alignment scoring."""
        if len(points) <= max_points:
            return points
        idx = np.linspace(0, len(points) - 1, max_points).astype(int)
        return points[idx]

    @staticmethod
    def _rotation_matrix(degrees: int) -> np.ndarray:
        """Return a 2D rotation matrix."""
        radians = np.deg2rad(degrees)
        c, s = np.cos(radians), np.sin(radians)
        return np.array([[c, -s], [s, c]], dtype=np.float64)
