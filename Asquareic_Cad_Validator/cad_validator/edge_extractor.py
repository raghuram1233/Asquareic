"""
Edge Extractor
==============

Extracts 2D Edge2D entities from the HLR results.
Traverses TopoDS_Shape edges, adapts each curve to its 2D type,
and discretizes them into point arrays.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, TYPE_CHECKING

import numpy as np           #type: ignore
from .geometry_store import Edge2D, EdgeType, ViewGeometry, ViewName
from .hlr_engine import HLRResult

if TYPE_CHECKING:
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopoDS import TopoDS
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_UniformDeflection
    from OCP.GeomAbs import (
        GeomAbs_Line,
        GeomAbs_Circle,
        GeomAbs_Ellipse,
        GeomAbs_BSplineCurve,
        GeomAbs_BezierCurve,
    )
else:
    try:
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_EDGE
        from OCP.TopoDS import TopoDS
        from OCP.BRepAdaptor import BRepAdaptor_Curve
        from OCP.GCPnts import GCPnts_UniformDeflection
        from OCP.GeomAbs import (
            GeomAbs_Line,
            GeomAbs_Circle,
            GeomAbs_Ellipse,
            GeomAbs_BSplineCurve,
            GeomAbs_BezierCurve,
        )
        _OCP_AVAILABLE = True
    except ModuleNotFoundError:
        TopExp_Explorer = None
        TopAbs_EDGE = None
        TopoDS = None
        BRepAdaptor_Curve = None
        GCPnts_UniformDeflection = None
        GeomAbs_Line = None
        GeomAbs_Circle = None
        GeomAbs_Ellipse = None
        GeomAbs_BSplineCurve = None
        GeomAbs_BezierCurve = None
        _OCP_AVAILABLE = False

_OCP_IMPORT_ERROR = (
    "Missing OpenCascade Python bindings (OCP). "
    "Install with `pip install cadquery-ocp>=7.7` and ensure the environment is activated."
)


def _require_ocp() -> None:
    if not _OCP_AVAILABLE:
        raise ModuleNotFoundError(_OCP_IMPORT_ERROR)


logger = logging.getLogger(__name__)

# Default discretization deflection (mm)
DEFAULT_DEFLECTION = 0.01


class EdgeExtractor:
    """
    Extracts Edge2D objects from HLR results.

    Traverses the visible-edge compounds, classifies each edge
    by its underlying curve type, extracts analytic parameters,
    and discretizes the curve into a polyline.
    """

    def __init__(self, deflection: float = DEFAULT_DEFLECTION):
        """
        Args:
            deflection: Chord deflection for curve discretization.
                        Smaller = more points = higher fidelity.
        """
        self.deflection = deflection

    def extract(
        self,
        hlr_result: HLRResult,
        include_hidden: bool = False,
    ) -> ViewGeometry:
        """
        Extract 2D edges from an HLR result.

        Args:
            hlr_result:     The HLR result for one view.
            include_hidden: If True, also extract hidden edges.

        Returns:
            A ViewGeometry containing all extracted Edge2D entities.
        """
        _require_ocp()
        view_geom = ViewGeometry(
            view_name=hlr_result.view_name,
            source="model",
        )

        # Process visible edges
        for shape in hlr_result.get_visible_shapes():
            edges = self._extract_from_shape(shape)
            view_geom.edges.extend(edges)

        # Optionally process hidden edges
        if include_hidden:
            for shape in hlr_result.get_hidden_shapes():
                edges = self._extract_from_shape(shape)
                view_geom.edges.extend(edges)

        logger.info(
            "Extracted %d edges for %s view",
            len(view_geom.edges),
            hlr_result.view_name.value,
        )
        return view_geom

    def extract_all(
        self,
        hlr_results: Dict[ViewName, HLRResult],
        include_hidden: bool = False,
    ) -> Dict[ViewName, ViewGeometry]:
        """Extract edges for all views."""
        return {
            view: self.extract(result, include_hidden)
            for view, result in hlr_results.items()
        }

    def _extract_from_shape(self, shape) -> List[Edge2D]:
        """Traverse a TopoDS_Shape and extract all edges."""
        edges = []
        _require_ocp()
        explorer = TopExp_Explorer(shape, TopAbs_EDGE)
        while explorer.More():
            topo_edge = TopoDS.Edge_s(explorer.Current())
            try:
                edge2d = self._process_edge(topo_edge)
                if edge2d is not None:
                    edges.append(edge2d)
            except Exception as e:
                logger.debug("Skipping edge due to error: %s", e)
            explorer.Next()
        return edges

    def _process_edge(self, topo_edge) -> Edge2D | None:
        """
        Convert a single TopoDS_Edge to an Edge2D.

        Adapts the curve, classifies its type, extracts parameters,
        and discretizes into points.
        """
        adaptor = BRepAdaptor_Curve(topo_edge)
        curve_type = adaptor.GetType()
        first = adaptor.FirstParameter()
        last = adaptor.LastParameter()

        # Skip degenerate edges
        if abs(last - first) < 1e-10:
            return None

        # Classify the edge type and extract parameters
        edge_type, params = self._classify_curve(adaptor, curve_type)

        # Discretize the curve
        points = self._discretize_curve(adaptor, first, last)
        if points is None or len(points) < 2:
            return None

        # Project 3D points to 2D (take X, Y from the HLR output which
        # is already in the projection plane)
        points_2d = points[:, :2]

        return Edge2D(
            edge_type=edge_type,
            points=points_2d,
            params=params,
            source="model",
        )

    def _classify_curve(self, adaptor, curve_type) -> tuple:
        """Classify the curve and extract analytic parameters."""
        params = {}

        if curve_type == GeomAbs_Line:
            edge_type = EdgeType.LINE
            p1 = adaptor.Value(adaptor.FirstParameter())
            p2 = adaptor.Value(adaptor.LastParameter())
            params = {
                "start": (p1.X(), p1.Y()),
                "end": (p2.X(), p2.Y()),
            }

        elif curve_type == GeomAbs_Circle:
            circle = adaptor.Circle()
            center = circle.Location()
            radius = circle.Radius()

            first = adaptor.FirstParameter()
            last = adaptor.LastParameter()

            # Full circle vs arc
            if abs(last - first - 2 * math.pi) < 1e-6:
                edge_type = EdgeType.CIRCLE
                params = {
                    "center": (center.X(), center.Y()),
                    "radius": radius,
                }
            else:
                edge_type = EdgeType.ARC
                params = {
                    "center": (center.X(), center.Y()),
                    "radius": radius,
                    "start_angle": first,
                    "end_angle": last,
                }

        elif curve_type == GeomAbs_Ellipse:
            edge_type = EdgeType.ELLIPSE
            ellipse = adaptor.Ellipse()
            center = ellipse.Location()
            params = {
                "center": (center.X(), center.Y()),
                "major_radius": ellipse.MajorRadius(),
                "minor_radius": ellipse.MinorRadius(),
            }

        elif curve_type in (GeomAbs_BSplineCurve, GeomAbs_BezierCurve):
            edge_type = EdgeType.BSPLINE
            if curve_type == GeomAbs_BSplineCurve:
                bspline = adaptor.BSpline()
                params = {
                    "degree": bspline.Degree(),
                    "nb_poles": bspline.NbPoles(),
                    "nb_knots": bspline.NbKnots(),
                }
            else:
                bezier = adaptor.Bezier()
                params = {
                    "degree": bezier.Degree(),
                    "nb_poles": bezier.NbPoles(),
                }

        else:
            # Generic / other curve types — treat as polyline
            edge_type = EdgeType.POLYLINE
            params = {"original_type": str(curve_type)}

        return edge_type, params

    def _discretize_curve(
        self,
        adaptor: BRepAdaptor_Curve,
        first: float,
        last: float,
    ) -> np.ndarray | None:
        """
        Discretize a curve into an array of 3D points using
        uniform deflection (chord-height based).
        """
        try:
            discretizer = GCPnts_UniformDeflection(
                adaptor, self.deflection, first, last
            )

            if not discretizer.IsDone():
                # Fallback: sample uniformly by parameter
                return self._fallback_discretize(adaptor, first, last)

            n_points = discretizer.NbPoints()
            if n_points < 2:
                return None

            points = np.empty((n_points, 3), dtype=np.float64)
            for i in range(1, n_points + 1):
                pt = discretizer.Value(i)
                points[i - 1] = [pt.X(), pt.Y(), pt.Z()]

            return points

        except Exception as e:
            logger.debug("Deflection discretization failed: %s. Using fallback.", e)
            return self._fallback_discretize(adaptor, first, last)

    @staticmethod
    def _fallback_discretize(
        adaptor: BRepAdaptor_Curve,
        first: float,
        last: float,
        n_samples: int = 64,
    ) -> np.ndarray | None:
        """Uniform parameter sampling as a fallback discretization."""
        params = np.linspace(first, last, n_samples)
        points = np.empty((n_samples, 3), dtype=np.float64)
        for i, t in enumerate(params):
            pt = adaptor.Value(t)
            points[i] = [pt.X(), pt.Y(), pt.Z()]
        return points
