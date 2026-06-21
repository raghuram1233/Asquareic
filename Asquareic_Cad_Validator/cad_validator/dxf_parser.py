"""
DXF Parser
==========

Parses 2D engineering drawings in DXF format using ezdxf.
Extracts geometric entities (LINE, ARC, CIRCLE, LWPOLYLINE, SPLINE, ELLIPSE)
and converts them to the unified Edge2D representation.

Supports automatic view detection by analyzing spatial clusters of entities
in model space.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import ezdxf           #type: ignore
import numpy as np           #type: ignore
from scipy.cluster.hierarchy import fcluster, linkage            #type: ignore

from .geometry_store import Edge2D, EdgeType, ViewGeometry, ViewName

logger = logging.getLogger(__name__)


class DXFParseError(Exception):
    """Raised when DXF parsing fails."""
    pass


class DXFParser:
    """
    Parses a single DXF file and extracts all 2D geometry as a specific view.
    """

    # Number of points to discretize arcs/circles/splines
    ARC_DISCRETIZATION = 64
    SPLINE_DISCRETIZATION = 100

    def __init__(self, filepath: str | Path):
        self.filepath = Path(filepath)
        if not self.filepath.exists():
            raise FileNotFoundError(f"DXF file not found: {self.filepath}")
        self._doc = None
        self._msp = None

    def parse(self, view_name: ViewName = ViewName.FRONT) -> Dict[ViewName, ViewGeometry]:
        """
        Parse the DXF file and extract all entities as a single view geometry.
        """
        logger.info("Parsing DXF file: %s for view: %s", self.filepath, view_name.value)

        self._doc = ezdxf.readfile(str(self.filepath))
        self._msp = self._doc.modelspace()

        # Extract all 2D entities
        all_edges = self._extract_all_entities()
        logger.info("Extracted %d raw entities from DXF", len(all_edges))

        if not all_edges:
            logger.warning("No entities found in DXF file")
            return {}

        vg = ViewGeometry(view_name=view_name, edges=all_edges, source="drawing")
        return {view_name: vg}

    def _extract_all_entities(self) -> List[Edge2D]:
        """Extract all supported 2D entities from model space."""
        edges = []

        skipped_counts: Dict[str, int] = {}

        for entity in self._msp:
            try:
                entity_edges = self._parse_entity(entity)

                if not entity_edges:
                    entity_edges = self._parse_virtual_entities(entity)

                if not entity_edges:
                    dxf_type = entity.dxftype()
                    skipped_counts[dxf_type] = skipped_counts.get(dxf_type, 0) + 1
                    continue

                for edge in entity_edges:
                    edge.source = "drawing"
                    # Store layer info for layer-based view assignment
                    edge.params["_layer"] = entity.dxf.layer
                    edges.append(edge)

            except Exception as e:
                dxf_type = entity.dxftype()
                skipped_counts[dxf_type] = skipped_counts.get(dxf_type, 0) + 1
                logger.debug("Skipping entity %s: %s", dxf_type, e)

        if skipped_counts:
            logger.info("Skipped unsupported/unresolved DXF entities: %s", skipped_counts)

        return edges

    # ------------------------------------------------------------------
    # Entity parsers
    # ------------------------------------------------------------------

    def _parse_entity(self, entity) -> List[Edge2D]:
        """Parse a supported DXF entity into one or more edges."""
        dxf_type = entity.dxftype()
        edge = None

        if dxf_type == "LINE":
            edge = self._parse_line(entity)
        elif dxf_type == "ARC":
            edge = self._parse_arc(entity)
        elif dxf_type == "CIRCLE":
            edge = self._parse_circle(entity)
        elif dxf_type == "LWPOLYLINE":
            virtual_edges = self._parse_virtual_entities(entity)
            if virtual_edges:
                return virtual_edges
            edge = self._parse_lwpolyline(entity)
        elif dxf_type == "POLYLINE":
            virtual_edges = self._parse_virtual_entities(entity)
            if virtual_edges:
                return virtual_edges
            edge = self._parse_polyline(entity)
        elif dxf_type == "SPLINE":
            edge = self._parse_spline(entity)
        elif dxf_type == "ELLIPSE":
            edge = self._parse_ellipse(entity)

        return [edge] if edge is not None else []

    def _parse_virtual_entities(self, entity) -> List[Edge2D]:
        """
        Expand complex DXF entities into primitive virtual entities.

        This catches geometry stored as INSERTs, DIMENSION renderings, and
        bulged polylines that would otherwise be partially lost.
        """
        if not hasattr(entity, "virtual_entities"):
            return []

        edges: List[Edge2D] = []
        try:
            virtual_entities = list(entity.virtual_entities())
        except Exception as e:
            logger.debug("Virtual entity expansion failed for %s: %s",
                         entity.dxftype(), e)
            return []

        for virtual_entity in virtual_entities:
            try:
                for edge in self._parse_entity(virtual_entity):
                    edge.params["_source_entity"] = entity.dxftype()
                    edges.append(edge)
            except Exception as e:
                logger.debug("Skipping virtual entity %s from %s: %s",
                             virtual_entity.dxftype(), entity.dxftype(), e)

        return edges

    def _parse_line(self, entity) -> Edge2D:
        """Parse a LINE entity."""
        start = entity.dxf.start
        end = entity.dxf.end
        points = np.array([
            [start.x, start.y],
            [end.x, end.y],
        ])
        return Edge2D(
            edge_type=EdgeType.LINE,
            points=points,
            params={
                "start": (start.x, start.y),
                "end": (end.x, end.y),
            },
        )

    def _parse_arc(self, entity) -> Edge2D:
        """Parse an ARC entity."""
        cx, cy = entity.dxf.center.x, entity.dxf.center.y
        radius = entity.dxf.radius
        start_angle = math.radians(entity.dxf.start_angle)
        end_angle = math.radians(entity.dxf.end_angle)

        # Handle angle wrapping
        if end_angle <= start_angle:
            end_angle += 2 * math.pi

        angles = np.linspace(start_angle, end_angle, self.ARC_DISCRETIZATION)
        points = np.column_stack([
            cx + radius * np.cos(angles),
            cy + radius * np.sin(angles),
        ])

        return Edge2D(
            edge_type=EdgeType.ARC,
            points=points,
            params={
                "center": (cx, cy),
                "radius": radius,
                "start_angle": start_angle,
                "end_angle": end_angle,
            },
        )

    def _parse_circle(self, entity) -> Edge2D:
        """Parse a CIRCLE entity."""
        cx, cy = entity.dxf.center.x, entity.dxf.center.y
        radius = entity.dxf.radius

        angles = np.linspace(0, 2 * math.pi, self.ARC_DISCRETIZATION, endpoint=False)
        points = np.column_stack([
            cx + radius * np.cos(angles),
            cy + radius * np.sin(angles),
        ])
        # Close the circle
        points = np.vstack([points, points[0:1]])

        return Edge2D(
            edge_type=EdgeType.CIRCLE,
            points=points,
            params={
                "center": (cx, cy),
                "radius": radius,
            },
        )

    def _parse_lwpolyline(self, entity) -> Edge2D:
        """Parse a LWPOLYLINE entity."""
        with entity.points() as pts:
            coords = list(pts)

        if len(coords) < 2:
            return None

        points = np.array([[p[0], p[1]] for p in coords])

        if entity.closed:
            points = np.vstack([points, points[0:1]])

        return Edge2D(
            edge_type=EdgeType.POLYLINE,
            points=points,
            params={"closed": entity.closed},
        )

    def _parse_polyline(self, entity) -> Edge2D:
        """Parse a 2D POLYLINE entity."""
        vertices = list(entity.vertices)
        if len(vertices) < 2:
            return None

        points = np.array([
            [v.dxf.location.x, v.dxf.location.y]
            for v in vertices
        ])

        if entity.is_closed:
            points = np.vstack([points, points[0:1]])

        return Edge2D(
            edge_type=EdgeType.POLYLINE,
            points=points,
            params={"closed": entity.is_closed},
        )

    def _parse_spline(self, entity) -> Edge2D:
        """Parse a SPLINE entity using ezdxf's flattening."""
        try:
            # Use ezdxf's built-in spline flattening
            flat_points = list(entity.flattening(0.01))
            if len(flat_points) < 2:
                return None

            points = np.array([[p.x, p.y] for p in flat_points])

            params = {
                "degree": entity.dxf.degree,
            }
            if entity.control_points:
                params["nb_poles"] = len(entity.control_points)
            if entity.knots:
                params["nb_knots"] = len(entity.knots)

            return Edge2D(
                edge_type=EdgeType.BSPLINE,
                points=points,
                params=params,
            )
        except Exception as e:
            logger.debug("Spline flattening failed: %s", e)
            return None

    def _parse_ellipse(self, entity) -> Edge2D:
        """Parse an ELLIPSE entity."""
        try:
            flat_points = list(entity.flattening(0.01))
            if len(flat_points) < 2:
                return None

            points = np.array([[p.x, p.y] for p in flat_points])

            center = entity.dxf.center
            return Edge2D(
                edge_type=EdgeType.ELLIPSE,
                points=points,
                params={
                    "center": (center.x, center.y),
                    "ratio": entity.dxf.ratio,
                },
            )
        except Exception as e:
            logger.debug("Ellipse flattening failed: %s", e)
            return None


