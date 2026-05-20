"""
STEP Loader
============

Loads 3D STEP files and extracts B-Rep topology using OpenCascade.
Provides shape validation, healing, and metadata extraction.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from OCP.BRep import BRep_Builder
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_Reader
    from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX, TopAbs_SOLID
    from OCP.ShapeFix import ShapeFix_Shape
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
else:
    try:
        from OCP.BRep import BRep_Builder
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Interface import Interface_Static
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_VERTEX, TopAbs_SOLID
        from OCP.ShapeFix import ShapeFix_Shape
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        _OCP_AVAILABLE = True
    except ModuleNotFoundError:
        BRep_Builder = None
        BRepCheck_Analyzer = None
        IFSelect_RetDone = None
        Interface_Static = None
        STEPControl_Reader = None
        TopoDS_Compound = None
        TopoDS_Shape = None
        TopExp_Explorer = None
        TopAbs_FACE = None
        TopAbs_EDGE = None
        TopAbs_VERTEX = None
        TopAbs_SOLID = None
        ShapeFix_Shape = None
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


class STEPLoadError(Exception):
    """Raised when STEP file loading fails."""
    pass


class STEPLoader:
    """
    Loads and validates a 3D STEP file, returning a healed TopoDS_Shape.

    Usage:
        loader = STEPLoader("path/to/model.step")
        shape = loader.load()
        info = loader.get_shape_info()
    """

    def __init__(self, filepath: str | Path):
        self.filepath = Path(filepath)
        if not self.filepath.exists():
            raise FileNotFoundError(f"STEP file not found: {self.filepath}")
        if self.filepath.suffix.upper() not in (".STEP", ".STP"):
            raise STEPLoadError(
                f"Invalid file extension: {self.filepath.suffix}. "
                "Expected .STEP or .STP"
            )
        self._shape: Optional[TopoDS_Shape] = None
        self._healed: bool = False

    def load(self, heal: bool = True) -> TopoDS_Shape:
        """
        Read the STEP file and return the resulting shape.

        Args:
            heal: If True, apply shape healing/fixing after load.

        Returns:
            A TopoDS_Shape (possibly a Compound of multiple bodies).
        """
        _require_ocp()
        logger.info("Loading STEP file: %s", self.filepath)

        reader = STEPControl_Reader()

        # Set reader parameters for better compatibility
        Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")

        status = reader.ReadFile(str(self.filepath))
        if status != IFSelect_RetDone:
            raise STEPLoadError(
                f"Failed to read STEP file (status={status}): {self.filepath}"
            )

        # Transfer all roots
        num_roots = reader.NbRootsForTransfer()
        logger.info("STEP file contains %d root(s)", num_roots)

        transfer_count = reader.TransferRoots()
        if transfer_count == 0:
            raise STEPLoadError("No shapes transferred from STEP file")

        logger.info("Transferred %d shape(s)", transfer_count)

        # Build compound if multiple shapes
        if reader.NbShapes() == 1:
            self._shape = reader.OneShape()
        else:
            builder = BRep_Builder()
            compound = TopoDS_Compound()
            builder.MakeCompound(compound)
            for i in range(1, reader.NbShapes() + 1):
                builder.Add(compound, reader.Shape(i))
            self._shape = compound

        if self._shape is None or self._shape.IsNull():
            raise STEPLoadError("Loaded shape is null")

        # Heal the shape
        if heal:
            self._shape = self._heal_shape(self._shape)
            self._healed = True

        logger.info("STEP file loaded successfully")
        return self._shape

    @staticmethod
    def _heal_shape(shape: TopoDS_Shape) -> TopoDS_Shape:
        """Apply shape fixing to repair B-Rep topology issues."""
        logger.info("Healing shape...")
        fixer = ShapeFix_Shape(shape)
        fixer.Perform()
        healed = fixer.Shape()
        if healed is None or healed.IsNull():
            logger.warning("Shape healing returned null; using original")
            return shape
        return healed

    def validate(self) -> bool:
        """
        Run BRepCheck_Analyzer on the loaded shape.

        Returns:
            True if the shape is valid, False otherwise.
        """
        if self._shape is None:
            raise RuntimeError("No shape loaded. Call load() first.")
        analyzer = BRepCheck_Analyzer(self._shape)
        is_valid = analyzer.IsValid()
        if not is_valid:
            logger.warning("Shape validation FAILED — geometry may have defects")
        else:
            logger.info("Shape validation PASSED")
        return is_valid

    def get_shape_info(self) -> dict:
        """
        Extract topology statistics and bounding box from the loaded shape.

        Returns:
            Dictionary with counts of solids, faces, edges, vertices
            and the axis-aligned bounding box.
        """
        _require_ocp()
        if self._shape is None:
            raise RuntimeError("No shape loaded. Call load() first.")

        info = {
            "file": str(self.filepath),
            "healed": self._healed,
            "solids": self._count_topology(TopAbs_SOLID),
            "faces": self._count_topology(TopAbs_FACE),
            "edges": self._count_topology(TopAbs_EDGE),
            "vertices": self._count_topology(TopAbs_VERTEX),
        }

        # Bounding box
        bbox = Bnd_Box()
        BRepBndLib.Add_s(self._shape, bbox)
        if not bbox.IsVoid():
            xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
            info["bounding_box"] = {
                "min": (round(xmin, 4), round(ymin, 4), round(zmin, 4)),
                "max": (round(xmax, 4), round(ymax, 4), round(zmax, 4)),
                "size": (
                    round(xmax - xmin, 4),
                    round(ymax - ymin, 4),
                    round(zmax - zmin, 4),
                ),
            }

        logger.info("Shape info: %s", info)
        return info

    def _count_topology(self, topo_type) -> int:
        """Count topological entities of a given type."""
        _require_ocp()
        explorer = TopExp_Explorer(self._shape, topo_type)
        count = 0
        while explorer.More():
            count += 1
            explorer.Next()
        return count

    @property
    def shape(self) -> TopoDS_Shape:
        if self._shape is None:
            raise RuntimeError("No shape loaded. Call load() first.")
        return self._shape
