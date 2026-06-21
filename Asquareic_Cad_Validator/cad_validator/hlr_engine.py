"""
HLR Engine
==========

Performs Hidden Line Removal on a 3D B-Rep shape for a given projector.
Separates visible and hidden edges in the projection plane.
"""

from __future__ import annotations

import logging
from typing import Dict, TYPE_CHECKING

from .geometry_store import ViewName

if TYPE_CHECKING:
    from OCP.HLRAlgo import HLRAlgo_Projector
    from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
    from OCP.TopoDS import TopoDS_Shape
else:
    try:
        from OCP.HLRAlgo import HLRAlgo_Projector
        from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
        from OCP.TopoDS import TopoDS_Shape
        _OCP_AVAILABLE = True
    except ModuleNotFoundError:
        HLRAlgo_Projector = None
        HLRBRep_Algo = None
        HLRBRep_HLRToShape = None
        TopoDS_Shape = None
        _OCP_AVAILABLE = False

_OCP_IMPORT_ERROR = (
    "Missing OpenCascade Python bindings (OCP). "
    "Install with `pip install cadquery-ocp>=7.7` and ensure the environment is activated."
)


def _require_ocp() -> None:
    if not _OCP_AVAILABLE:
        raise ModuleNotFoundError(_OCP_IMPORT_ERROR)


logger = logging.getLogger(__name__)


class HLRResult:
    """
    Container for the result of Hidden Line Removal.

    Holds TopoDS_Shape results for visible and hidden edges,
    separated by edge type (sharp, smooth, sewn, outline).
    """

    def __init__(self, view_name: ViewName, hlr_to_shape: HLRBRep_HLRToShape):
        self.view_name = view_name
        self._hlr = hlr_to_shape

        # Visible edges
        self.visible_sharp: TopoDS_Shape = hlr_to_shape.VCompound()
        self.visible_smooth: TopoDS_Shape = hlr_to_shape.Rg1LineVCompound()
        self.visible_sewn: TopoDS_Shape = hlr_to_shape.RgNLineVCompound()
        self.visible_outline: TopoDS_Shape = hlr_to_shape.OutLineVCompound()
        self.visible_iso: TopoDS_Shape = hlr_to_shape.IsoLineVCompound()

        # Hidden edges
        self.hidden_sharp: TopoDS_Shape = hlr_to_shape.HCompound()
        self.hidden_smooth: TopoDS_Shape = hlr_to_shape.Rg1LineHCompound()
        self.hidden_sewn: TopoDS_Shape = hlr_to_shape.RgNLineHCompound()
        self.hidden_outline: TopoDS_Shape = hlr_to_shape.OutLineHCompound()
        self.hidden_iso: TopoDS_Shape = hlr_to_shape.IsoLineHCompound()

    def get_visible_shapes(self) -> list[TopoDS_Shape]:
        """Return all non-null visible edge shapes."""
        shapes = []
        for s in [self.visible_sharp, self.visible_smooth,
                  self.visible_sewn, self.visible_outline,
                  self.visible_iso]:
            if s is not None and not s.IsNull():
                shapes.append(s)
        return shapes

    def get_hidden_shapes(self) -> list[TopoDS_Shape]:
        """Return all non-null hidden edge shapes."""
        shapes = []
        for s in [self.hidden_sharp, self.hidden_smooth,
                  self.hidden_sewn, self.hidden_outline,
                  self.hidden_iso]:
            if s is not None and not s.IsNull():
                shapes.append(s)
        return shapes


class HLREngine:
    """
    Runs the OpenCascade HLRBRep algorithm on a shape.

    Usage:
        engine = HLREngine(shape)
        result = engine.compute(view_name, projector)
    """

    def __init__(self, shape: TopoDS_Shape):
        _require_ocp()
        self._shape = shape

    def compute(
        self,
        view_name: ViewName,
        projector: HLRAlgo_Projector,
    ) -> HLRResult:
        """
        Run HLR for a single view.

        Args:
            view_name:  The view identifier.
            projector:  An HLRAlgo_Projector defining the projection.

        Returns:
            HLRResult containing visible and hidden edge shapes.
        """
        logger.info("Running HLR for view: %s", view_name.value)

        algo = HLRBRep_Algo()
        algo.Add(self._shape)
        algo.Projector(projector)
        algo.Update()
        algo.Hide()

        hlr_to_shape = HLRBRep_HLRToShape(algo)

        result = HLRResult(view_name, hlr_to_shape)

        vis_count = len(result.get_visible_shapes())
        hid_count = len(result.get_hidden_shapes())
        logger.info("HLR %s: %d visible compound(s), %d hidden compound(s)",
                     view_name.value, vis_count, hid_count)

        return result

    def compute_all(
        self,
        projectors: Dict[ViewName, HLRAlgo_Projector],
    ) -> Dict[ViewName, HLRResult]:
        """
        Run HLR for all provided views.

        Args:
            projectors: Dict mapping ViewName to its projector.

        Returns:
            Dict mapping ViewName to its HLRResult.
        """
        results = {}
        for view_name, projector in projectors.items():
            results[view_name] = self.compute(view_name, projector)
        return results
