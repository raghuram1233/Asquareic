"""
CAD Validation System
=====================

Industrial-grade geometry-based comparison of 3D STEP models
against 2D engineering drawings (DXF format).

Modules:
    - step_loader:        Load 3D STEP B-Rep shapes
    - projector:          Generate orthographic projections
    - hlr_engine:         Hidden line removal
    - edge_extractor:     Extract 2D edges from HLR results
    - dxf_parser:         Parse 2D DXF drawing entities
    - geometry_store:     Unified 2D geometry representation
    - normalizer:         Normalize, align, and scale geometry
    - image_comparator:   Image-based crop-align-overlay comparison
    - matcher:            Entity-level geometric matching
    - diff_detector:      Detect and classify differences
    - reporter:           Visual + structured report output
    - pipeline:           End-to-end orchestration
"""

__version__ = "1.1.0"

