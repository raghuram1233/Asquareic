"""
Geometry Store
==============

Unified 2D geometry data structures used throughout the pipeline.
All modules produce and consume these structures for interoperability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np


class EdgeType(str, Enum):
    """Supported 2D edge primitive types."""
    LINE = "line"
    ARC = "arc"
    CIRCLE = "circle"
    ELLIPSE = "ellipse"
    BSPLINE = "bspline"
    POLYLINE = "polyline"


class ViewName(str, Enum):
    """Standard orthographic views."""
    FRONT = "front"
    BACK = "back"
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"


# ---------------------------------------------------------------------------
# View direction definitions (ISO standard first-angle / third-angle agnostic)
# Each view is defined by a projection direction and an up-vector.
# The projection direction points FROM the observer TOWARD the object.
# ---------------------------------------------------------------------------
VIEW_DEFINITIONS: Dict[ViewName, Dict[str, Tuple[float, float, float]]] = {
    ViewName.FRONT:  {"direction": (0.0, -1.0, 0.0),  "up": (0.0, 0.0, 1.0)},
    ViewName.BACK:   {"direction": (0.0,  1.0, 0.0),  "up": (0.0, 0.0, 1.0)},
    ViewName.TOP:    {"direction": (0.0,  0.0,  1.0), "up": (0.0, -1.0, 0.0)},
    ViewName.BOTTOM: {"direction": (0.0,  0.0, -1.0), "up": (0.0, 1.0, 0.0)},
    ViewName.LEFT:   {"direction": (1.0,  0.0, 0.0),  "up": (0.0, 0.0, 1.0)},
    ViewName.RIGHT:  {"direction": (-1.0, 0.0, 0.0),  "up": (0.0, 0.0, 1.0)},
}


@dataclass
class Edge2D:
    """
    A single 2D edge entity with discretized point representation.

    Attributes:
        edge_id:    Unique identifier for this edge.
        edge_type:  Primitive type of the edge.
        points:     (N, 2) array of discretized 2D points along the edge.
        params:     Type-specific analytic parameters.
                    - line:   {'start': (x,y), 'end': (x,y)}
                    - arc:    {'center': (x,y), 'radius': float,
                               'start_angle': float, 'end_angle': float}
                    - circle: {'center': (x,y), 'radius': float}
                    - bspline: {'degree': int, 'knots': list, 'poles': list}
        source:     Origin of this edge ('model' or 'drawing').
        length:     Approximate arc-length of this edge.
    """
    edge_type: EdgeType
    points: np.ndarray
    params: Dict = field(default_factory=dict)
    source: str = "model"
    edge_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    length: float = 0.0

    def __post_init__(self):
        self.points = np.asarray(self.points, dtype=np.float64)
        if self.points.ndim == 1:
            self.points = self.points.reshape(-1, 2)
        if self.length == 0.0 and len(self.points) > 1:
            diffs = np.diff(self.points, axis=0)
            self.length = float(np.sum(np.linalg.norm(diffs, axis=1)))

    @property
    def centroid(self) -> np.ndarray:
        """Geometric centroid of the discretized points."""
        return np.mean(self.points, axis=0)

    @property
    def bounding_box(self) -> Tuple[float, float, float, float]:
        """(min_x, min_y, max_x, max_y)."""
        mins = self.points.min(axis=0)
        maxs = self.points.max(axis=0)
        return (mins[0], mins[1], maxs[0], maxs[1])


@dataclass
class ViewGeometry:
    """
    Collection of 2D edges belonging to a single orthographic view.

    Attributes:
        view_name:      Which standard view this represents.
        edges:          List of Edge2D entities.
        source:         'model' or 'drawing'.
        bounding_box:   Combined bounding box (min_x, min_y, max_x, max_y).
    """
    view_name: ViewName
    edges: List[Edge2D] = field(default_factory=list)
    source: str = "model"

    @property
    def bounding_box(self) -> Optional[Tuple[float, float, float, float]]:
        if not self.edges:
            return None
        all_points = np.vstack([e.points for e in self.edges])
        mins = all_points.min(axis=0)
        maxs = all_points.max(axis=0)
        return (float(mins[0]), float(mins[1]), float(maxs[0]), float(maxs[1]))

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    @property
    def total_length(self) -> float:
        return sum(e.length for e in self.edges)


@dataclass
class MatchPair:
    """A pair of matched edges between model and drawing."""
    model_edge: Edge2D
    drawing_edge: Edge2D
    distance: float          # Hausdorff or mean distance
    confidence: float        # 0.0 - 1.0


@dataclass
class DiffResult:
    """
    Geometric comparison result for a single orthographic view.
    """
    view_name: ViewName
    missing_in_drawing: List[Edge2D] = field(default_factory=list)
    extra_in_drawing: List[Edge2D] = field(default_factory=list)
    modified: List[MatchPair] = field(default_factory=list)
    matched: List[MatchPair] = field(default_factory=list)

    @property
    def match_score(self) -> float:
        """Overall match score for this view (0.0 to 1.0)."""
        total = (len(self.matched) + len(self.missing_in_drawing)
                 + len(self.extra_in_drawing) + len(self.modified))
        if total == 0:
            return 1.0
        return len(self.matched) / total

    @property
    def has_differences(self) -> bool:
        return bool(self.missing_in_drawing or self.extra_in_drawing
                    or self.modified)

    def summary(self) -> Dict:
        return {
            "view": self.view_name.value,
            "matched": len(self.matched),
            "missing_in_drawing": len(self.missing_in_drawing),
            "extra_in_drawing": len(self.extra_in_drawing),
            "modified": len(self.modified),
            "match_score": round(self.match_score, 4),
        }


@dataclass
class ValidationReport:
    """Full validation report across all views."""
    step_file: str
    dxf_file: str
    view_results: Dict[ViewName, DiffResult] = field(default_factory=dict)

    @property
    def overall_score(self) -> float:
        if not self.view_results:
            return 0.0
        scores = [r.match_score for r in self.view_results.values()]
        return sum(scores) / len(scores)

    @property
    def passed(self) -> bool:
        return all(not r.has_differences for r in self.view_results.values())

    def summary(self) -> Dict:
        return {
            "step_file": self.step_file,
            "dxf_file": self.dxf_file,
            "overall_score": round(self.overall_score, 4),
            "passed": self.passed,
            "views": {
                v.value: r.summary()
                for v, r in self.view_results.items()
            },
        }
