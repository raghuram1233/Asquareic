"""
Normalizer
==========

Normalizes 2D geometry from both sources (model projection and DXF drawing)
into a common coordinate system for comparison.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Dict

import numpy as np

from .geometry_store import ViewGeometry, ViewName

logger = logging.getLogger(__name__)

class Normalizer:
    """
    Fits ViewGeometry instances so that DXF drawings match model projections
    in size and position without changing drawing orientation.
    """

    def __init__(self, drawing_scale: float = 1.0):
        if drawing_scale <= 0:
            raise ValueError("drawing_scale must be greater than zero")
        self.drawing_scale = drawing_scale

    def fit_drawing_to_model(
        self,
        model_geom: ViewGeometry,
        drawing_geom: ViewGeometry,
    ) -> ViewGeometry:
        """Scale and translate drawing geometry without rotating or mirroring it."""
        if not model_geom.edges or not drawing_geom.edges:
            return deepcopy(drawing_geom)

        m_pts = self._all_points(model_geom)
        d_pts = self._all_points(drawing_geom)

        if len(m_pts) < 2 or len(d_pts) < 2:
            return deepcopy(drawing_geom)

        m_centroid = m_pts.mean(axis=0)
        d_scaled = d_pts * self.drawing_scale
        d_centroid = d_scaled.mean(axis=0)
        translation = m_centroid - d_centroid

        result = deepcopy(drawing_geom)
        for edge in result.edges:
            edge.points = edge.points * self.drawing_scale + translation
            
            if len(edge.points) > 1:
                diffs = np.diff(edge.points, axis=0)
                edge.length = float(np.sum(np.linalg.norm(diffs, axis=1)))
                
            edge.params["_alignment"] = {
                "rotation_degrees": 0,
                "mirror_x": False,
                "mirror_y": False,
                "scale": round(self.drawing_scale, 6),
                "translation": tuple(round(float(v), 6) for v in translation),
            }

        logger.info(
            "View %s alignment: preserved orientation, scale=%.6f, translate=(%.6f, %.6f)",
            drawing_geom.view_name.value,
            self.drawing_scale,
            translation[0],
            translation[1],
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

    @staticmethod
    def _all_points(view_geom: ViewGeometry) -> np.ndarray:
        """Return all edge points for a view."""
        point_sets = [e.points for e in view_geom.edges if len(e.points)]
        if not point_sets:
            return np.empty((0, 2), dtype=np.float64)
        return np.vstack(point_sets)
