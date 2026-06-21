# Asquareic CAD Validator

A CAD validation system for comparing 3D STEP models against 2D engineering drawings (DXF).

## Installation

This project depends on Python packages that include OpenCascade bindings.

1. Create and activate your Python environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

If the `OCP` module is missing, install the OpenCascade bindings:

```bash
pip install cadquery-ocp>=7.7
```

## Usage

Run validation with a STEP model and DXF drawing folder:

```bash
python main.py Data/Model.STEP Data2 --output results
```

The script expects per-view DXF files named like `front_view.dxf`, `top_view.dxf`, etc.

## Notes

- The repository now handles missing OCP imports gracefully at import time.
- If OpenCascade is not available, the package imports cleanly and will raise a clear error when CAD processing begins.
