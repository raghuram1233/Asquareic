"""
Projector
=========

Generates orthographic projection setups for the 6 standard views.
Creates the camera/projector configuration that feeds into the HLR engine.
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple, TYPE_CHECKING

import numpy as np
from .geometry_store import ViewName, VIEW_DEFINITIONS

if TYPE_CHECKING:
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.TopoDS import TopoDS_Shape
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
else:
    try:
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
        from OCP.HLRAlgo import HLRAlgo_Projector
        from OCP.TopoDS import TopoDS_Shape
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        _OCP_AVAILABLE = True
    except ModuleNotFoundError:
        gp_Ax2 = None
        gp_Dir = None
        gp_Pnt = None
        HLRAlgo_Projector = None
        TopoDS_Shape = None
        Bnd_Box = None
        BRepBndLib = None
        _OCP_AVAILABLE = False

_OCP_IMPORT_ERROR = (
    "Missing OpenCascade Python bindings (OCP). "
    "Install with `pip install cadquery-ocp>=7.7` and ensure the environment is activated."
)


def _require_ocp() -> None:
    if not _OCP_AVAILABLE:
        raise ModuleNotFoundError(_OCP_IMPORT_ERROR)


logger = logging.getLogger(__name__)


class Projector:
    """
    Creates HLRAlgo_Projector instances for each standard orthographic view.

    The projector encodes the view direction and up-vector as an Ax2
    coordinate system, which the HLR algorithm uses to determine
    visible/hidden edges.
    """

    def __init__(self, shape: TopoDS_Shape):
        _require_ocp()
        self._shape = shape
        self._center = self._compute_center()
        logger.info("Shape center: (%.4f, %.4f, %.4f)",
                     self._center[0], self._center[1], self._center[2])

    def _compute_center(self) -> Tuple[float, float, float]:
        """Compute the center of the shape's bounding box."""
        _require_ocp()
        bbox = Bnd_Box()
        BRepBndLib.Add_s(self._shape, bbox)
        xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
        return (
            (xmin + xmax) / 2.0,
            (ymin + ymax) / 2.0,
            (zmin + zmax) / 2.0,
        )

    def create_projector(self, view: ViewName) -> HLRAlgo_Projector:
        """
        Create an HLR projector for the given standard view.

        The projector defines an orthographic (parallel) projection
        looking in the specified direction with the specified up-vector.

        Args:
            view: One of the 6 standard orthographic views.

        Returns:
            An HLRAlgo_Projector configured for the given view.
        """
        _require_ocp()
        view_def = VIEW_DEFINITIONS[view]
        direction = view_def["direction"]
        up = view_def["up"]

        logger.info("Creating projector for %s view — direction=%s, up=%s",
                     view.value, direction, up)

        # The Ax2 for HLR:
        # - Origin: a point along the view direction far from the shape
        # - Z-axis (main direction): opposite of the view direction
        #   (HLR looks along -Z of the Ax2)
        # - Y-axis (up): the up vector
        # We place the eye point far away along the negative view direction
        scale = 1000.0
        eye_point = gp_Pnt(
            self._center[0] - direction[0] * scale,
            self._center[1] - direction[1] * scale,
            self._center[2] - direction[2] * scale,
        )

        # The main direction of the Ax2 is the view direction
        # (HLR projects along this axis)
        main_dir = gp_Dir(direction[0], direction[1], direction[2])
        up_dir = gp_Dir(up[0], up[1], up[2])

        ax2 = gp_Ax2(eye_point, main_dir, up_dir)

        projector = HLRAlgo_Projector(ax2)
        return projector

    def create_all_projectors(self) -> Dict[ViewName, HLRAlgo_Projector]:
        """
        Create HLR projectors for all 6 standard views.

        Returns:
            Dictionary mapping ViewName to its projector.
        """
        projectors = {}
        for view in ViewName:
            projectors[view] = self.create_projector(view)
        logger.info("Created projectors for all %d views", len(projectors))
        return projectors

    def get_projection_axes(self, view: ViewName) -> Dict[str, np.ndarray]:
        """
        Get the 2D projection coordinate system axes for a given view.

        Returns the right-vector (X-axis in 2D) and up-vector (Y-axis in 2D)
        derived from the view direction and up-vector.

        Returns:
            Dict with 'right' and 'up' as 3D numpy arrays.
        """
        view_def = VIEW_DEFINITIONS[view]
        direction = np.array(view_def["direction"], dtype=np.float64)
        up = np.array(view_def["up"], dtype=np.float64)

        # Right vector = up × direction (cross product)
        right = np.cross(up, direction)
        right = right / np.linalg.norm(right)

        return {
            "direction": direction,
            "up": up,
            "right": right,
        }
