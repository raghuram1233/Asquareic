"""
CAD Validation System — Simplified Main
========================================
Default execution:
    python main.py Data/Model.STEP Data2 --output results
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from cad_validator.geometry_store import ViewName
from cad_validator.pipeline import Pipeline, PipelineConfig


def setup_logging():
    """Configure basic logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def resolve_drawing_input(dxf_path: str, views: list[ViewName]) -> str | dict[ViewName, str]:
    """Resolve the drawing input directory to per-view files."""
    path = Path(dxf_path)
    if not path.is_dir():
        return str(path)

    drawing_files: dict[ViewName, str] = {}
    missing: list[Path] = []
    for view in views:
        view_file = path / f"{view.value}_view.dxf"
        if view_file.exists():
            drawing_files[view] = str(view_file)
        else:
            missing.append(view_file)

    if missing:
        print("Error: Missing per-view DXF file(s):")
        for missing_file in missing:
            print(f"  {missing_file}")
        sys.exit(1)

    return drawing_files


def main():
    setup_logging()

    # Simple manual argument parsing with defaults
    step_file = r"Data\Model.STEP"
    dxf_file = "Data2"
    output_dir = "results"

    args = sys.argv[1:]
    
    # Very basic parsing if user provides args
    if args and not args[0].startswith("-"):
        step_file = args[0]
    if len(args) > 1 and not args[1].startswith("-"):
        dxf_file = args[1]
    
    if "--output" in args:
        idx = args.index("--output")
        if idx + 1 < len(args):
            output_dir = args[idx + 1]

    # Validate inputs
    if not Path(step_file).exists():
        print(f"Error: STEP file not found: {step_file}")
        sys.exit(1)
    if not Path(dxf_file).exists():
        print(f"Error: DXF file/dir not found: {dxf_file}")
        sys.exit(1)

    selected_views = list(ViewName)
    drawing_input = resolve_drawing_input(dxf_file, selected_views)

    config = PipelineConfig(
        views=selected_views,
        output_dir=output_dir,
    )

    pipeline = Pipeline(config)

    try:
        report = pipeline.run(step_file, drawing_input)
    except Exception as e:
        logging.getLogger(__name__).error("Pipeline failed: %s", e, exc_info=True)
        sys.exit(1)

    # Print summary
    print("\n" + "=" * 60)
    print(f"  RESULT: {'PASSED ✓' if report.passed else 'FAILED ✗'}")
    print(f"  SCORE:  {report.overall_score:.1%}")
    print(f"  REPORT: {output_dir}/")
    print("=" * 60)

    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
