#!/usr/bin/env python3
import sys
import os
import argparse
import json
import logging
from pathlib import Path
from datetime import datetime

# Add the CAD Validator directory to python path
workspace_dir = Path(__file__).parent.parent.resolve()
cad_val_dir = workspace_dir / "Asquareic_Cad_Validator"
sys.path.append(str(cad_val_dir))

try:
    from cad_validator.geometry_store import ViewName
    from cad_validator.pipeline import Pipeline, PipelineConfig
    from gui import prepare_drawing_views
except ImportError as e:
    print(f"Error importing CAD Validator modules: {e}", file=sys.stderr)
    sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Run CAD Validator from backend")
    parser.add_argument("--step", required=True, help="Path to STEP model")
    parser.add_argument("--drawing", required=True, help="Path to drawing sheet (DWG/DXF)")
    parser.add_argument("--scale", type=float, default=1.0, help="Drawing scale factor")
    parser.add_argument("--rotations", default="{}", help="JSON string representing view rotations")
    parser.add_argument("--outdir", required=True, help="Output directory")

    args = parser.parse_args()

    # Configure logging to stdout
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logger = logging.getLogger("cad_runner")

    step_path = Path(args.step).resolve()
    drawing_path = Path(args.drawing).resolve()
    outdir_path = Path(args.outdir).resolve()

    logger.info(f"Starting CAD Validator validation pipeline")
    logger.info(f"STEP: {step_path}")
    logger.info(f"Drawing: {drawing_path}")
    logger.info(f"Output: {outdir_path}")
    logger.info(f"Scale: {args.scale}")

    # Parse rotations
    try:
        rotations_dict = json.loads(args.rotations)
    except json.JSONDecodeError:
        logger.error("Failed to parse rotations JSON, defaulting to empty.")
        rotations_dict = {}

    view_rotations = {}
    for view, deg in rotations_dict.items():
        try:
            view_rotations[ViewName(view)] = float(deg)
        except ValueError:
            logger.warning(f"Invalid view name or degree: {view} -> {deg}")

    try:
        outdir_path.mkdir(parents=True, exist_ok=True)
        
        logger.info("Preparing drawing views (converting DWG and splitting DXF)...")
        drawing_views = prepare_drawing_views(drawing_path, outdir_path, logging.getLogger("cad_validator.prepare"))

        config = PipelineConfig(
            views=list(ViewName),
            output_dir=str(outdir_path),
            drawing_scale=args.scale,
            drawing_view_rotations=view_rotations
        )

        logger.info("Running comparison pipeline...")
        pipeline = Pipeline(config)
        report = pipeline.run(str(step_path), drawing_views)

        logger.info("Validation completed successfully")
        logger.info(f"RESULT: {'PASSED' if report.passed else 'FAILED'}")
        logger.info(f"OVERALL SCORE: {report.overall_score:.1%}")

        # The pipeline automatically dumps validation_report.json to outdir.
        # We write a sentinel output to confirm.
        with open(outdir_path / "cad_runner_status.json", "w") as f:
            json.dump({
                "status": "completed",
                "passed": report.passed,
                "overall_score": report.overall_score,
                "timestamp": datetime.now().isoformat()
            }, f, indent=2)
        
        sys.exit(0)

    except Exception as e:
        logger.error(f"Error during CAD validation: {e}", exc_info=True)
        with open(outdir_path / "cad_runner_status.json", "w") as f:
            json.dump({
                "status": "failed",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }, f, indent=2)
        sys.exit(1)

if __name__ == "__main__":
    main()
