#!/usr/bin/env python3
import os
import sys
import uuid
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime
from flask import Flask, jsonify, request, send_file, render_template

# Resolve workspace directories
WORKSPACE_DIR = Path(__file__).parent.parent.resolve()
RUNS_DIR = Path(__file__).parent / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")

# Global task registry
# task_id -> { "status": "running"|"completed"|"failed", "type": "...", "output_dir": "...", "log_file": "...", "process": Popen }
active_tasks = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_dep(lib_name):
    """Check if a Python library can be imported."""
    try:
        __import__(lib_name)
        return True
    except ImportError:
        return False

def check_ocp():
    """Specifically check for OpenCascade bindings (OCP)."""
    try:
        import OCP
        return True
    except ImportError:
        return False

def find_oda_converter():
    """Find ODA File Converter on PATH or program files."""
    exe = shutil.which("ODAFileConverter")
    if exe:
        return exe
    candidates = sorted(
        Path("C:/Program Files/ODA").glob("ODAFileConverter*/ODAFileConverter.exe"),
        reverse=True
    )
    return str(candidates[0]) if candidates else None

def get_task_status(task_id):
    """Poll process and update status, fallback to disk for historical tasks."""
    task = active_tasks.get(task_id)
    if not task:
        # Check if the folder exists on disk (historical run)
        outdir = RUNS_DIR / task_id
        if outdir.exists() and outdir.is_dir():
            # Determine task type
            task_type = "unknown"
            if task_id.startswith("cad_val_"):
                task_type = "cad-validator"
            elif task_id.startswith("dim_chk_"):
                task_type = "dimension-checker"
            elif task_id.startswith("notes_chk_"):
                task_type = "notes-checker"
            elif task_id.startswith("nozzle_val_"):
                task_type = "nozzle-validator"
            
            # Determine status based on key output files
            status = "completed"
            error = None
            if task_type == "cad-validator":
                if not (outdir / "validation_report.json").exists():
                    status = "failed"
                    error = "Validation report missing"
            elif task_type == "dimension-checker":
                if not (outdir / "dimension_validation.png").exists():
                    status = "failed"
                    error = "Dimension validation image missing"
            elif task_type == "notes-checker":
                if not (outdir / "validation_report.csv").exists():
                    status = "failed"
                    error = "Validation report CSV missing"
            elif task_type == "nozzle-validator":
                if not (outdir / "nozzle_loads_table.csv").exists():
                    status = "failed"
                    error = "Nozzle loads table CSV missing"
                    
            return {
                "task_id": task_id,
                "type": task_type,
                "status": status,
                "output_dir": str(outdir),
                "log_file": str(outdir / "output.log"),
                "error": error
            }
        return None
        
    if task["status"] == "running":
        proc = task["process"]
        if proc is not None:
            ret = proc.poll()
            if ret is not None:
                if ret == 0:
                    task["status"] = "completed"
                else:
                    task["status"] = "failed"
                    task["error"] = f"Process exited with non-zero code: {ret}"
                
    # Create serializable copy (omit Popen object)
    return {
        "task_id": task_id,
        "type": task["type"],
        "status": task["status"],
        "output_dir": str(task["output_dir"]),
        "log_file": str(task["log_file"]),
        "error": task.get("error", None)
    }

# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    """System health check and dependencies diagnostic."""
    dependencies = {
        "flask": check_dep("flask"),
        "ezdxf": check_dep("ezdxf"),
        "numpy": check_dep("numpy"),
        "shapely": check_dep("shapely"),
        "scipy": check_dep("scipy"),
        "opencv-python (cv2)": check_dep("cv2"),
        "matplotlib": check_dep("matplotlib"),
        "python-docx (docx)": check_dep("docx"),
        "pandas": check_dep("pandas"),
        "openpyxl": check_dep("openpyxl"),
        "cadquery-ocp (OCP)": check_ocp()
    }
    
    oda_path = find_oda_converter()
    
    return jsonify({
        "status": "online",
        "workspace": str(WORKSPACE_DIR),
        "dependencies": dependencies,
        "oda_converter_path": oda_path or "Not Found"
    })

@app.route("/api/browse")
def browse():
    """Directory tree browser for local file selection dialog."""
    raw_path = request.args.get("path", "")
    
    if not raw_path:
        # Default to workspace root
        target_path = WORKSPACE_DIR
    else:
        target_path = Path(raw_path).resolve()
        
    if not target_path.exists() or not target_path.is_dir():
        return jsonify({"error": f"Path not found or is not a directory: {target_path}"}), 404
        
    try:
        dirs = []
        files = []
        
        # Allowed file extensions for selection
        allowed_exts = {".dwg", ".dxf", ".step", ".stp", ".docx", ".csv", ".xlsx", ".html", ".png"}
        
        for item in target_path.iterdir():
            if item.name.startswith(".") or item.name.startswith("__"):
                continue
                
            if item.is_dir():
                dirs.append({
                    "name": item.name,
                    "path": str(item.resolve())
                })
            elif item.is_file():
                ext = item.suffix.lower()
                if ext in allowed_exts:
                    files.append({
                        "name": item.name,
                        "path": str(item.resolve()),
                        "size": item.stat().st_size
                    })
                    
        # Sort lists alphabetically
        dirs.sort(key=lambda d: d["name"].lower())
        files.sort(key=lambda f: f["name"].lower())
        
        # Resolve parent directory
        parent_path = None
        if target_path != target_path.parent:
            parent_path = str(target_path.parent.resolve())
            
        return jsonify({
            "current_path": str(target_path.resolve()),
            "parent_path": parent_path,
            "dirs": dirs,
            "files": files
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/file")
def get_file():
    """Serve any file from host system safely (validating extensions)."""
    file_path = request.args.get("path", "")
    if not file_path:
        return jsonify({"error": "Path parameter is required"}), 400
        
    path = Path(file_path).resolve()
    if not path.exists() or not path.is_file():
        return jsonify({"error": "File not found"}), 404
        
    # Safe extension check
    allowed_exts = {".png", ".jpg", ".jpeg", ".html", ".csv", ".xlsx", ".json", ".txt"}
    if path.suffix.lower() not in allowed_exts:
        return jsonify({"error": f"File type {path.suffix} not allowed for viewing"}), 403
        
    return send_file(path)

# ---------------------------------------------------------------------------
# Task Runner APIs
# ---------------------------------------------------------------------------

@app.route("/api/task/<task_id>")
def task_status(task_id):
    """Retrieve status details for a given task ID."""
    status_data = get_task_status(task_id)
    if not status_data:
        return jsonify({"error": "Task not found"}), 404
        
    # Inject specific result metrics if completed
    if status_data["status"] == "completed":
        outdir = Path(status_data["output_dir"])
        task_type = status_data["type"]
        
        if task_type == "cad-validator":
            status_json = outdir / "cad_runner_status.json"
            report_json = outdir / "validation_report.json"
            if report_json.exists():
                try:
                    with open(report_json, "r") as f:
                        status_data["report"] = json.load(f)
                except Exception as e:
                    status_data["error"] = f"Failed to load CAD report data: {e}"
            elif status_json.exists():
                try:
                    with open(status_json, "r") as f:
                        status_data["report"] = json.load(f)
                except Exception:
                    pass
                    
        elif task_type == "dimension-checker":
            # Read stdout output from logs to parse dimension matches
            log_path = Path(status_data["log_file"])
            if log_path.exists():
                try:
                    log_text = log_path.read_text(encoding="utf-8")
                    # Parse matches lines
                    # e.g., "  [01] Dim:84.0  CAD:16.76  Type:line  -> OK"
                    import re
                    pattern = re.compile(r"\[(\d+)\]\s+Dim:([\d.-]+)\s+CAD:([\d.-]+)\s+Type:(\w+)\s+->\s+(.*)")
                    matches = []
                    
                    # Parse lines
                    summary_text = ""
                    for line in log_text.splitlines():
                        m = pattern.search(line)
                        if m:
                            matches.append({
                                "id": int(m.group(1)),
                                "dim": float(m.group(2)),
                                "cad": float(m.group(3)),
                                "type": m.group(4),
                                "status": m.group(5).strip()
                            })
                        if "Found" in line and "dimension texts" in line:
                            summary_text = line.strip()
                        if "Best tolerance:" in line:
                            summary_text += " | " + line.strip()
                            
                    status_data["results"] = {
                        "summary": summary_text,
                        "matches": matches,
                        "image_path": str(outdir / "dimension_validation.png")
                    }
                except Exception as e:
                    status_data["error"] = f"Failed to parse log results: {e}"
                    
        elif task_type == "notes-checker":
            csv_report = outdir / "validation_report.csv"
            if csv_report.exists():
                try:
                    import pandas as pd
                    df = pd.read_csv(csv_report)
                    status_data["results"] = df.to_dict(orient="records")
                except Exception as e:
                    status_data["error"] = f"Failed to load CSV results: {e}"
                    
        elif task_type == "nozzle-validator":
            csv_report = outdir / "nozzle_loads_table.csv"
            html_report = outdir / "nozzle_validation_report.html"
            if csv_report.exists():
                try:
                    import pandas as pd
                    df = pd.read_csv(csv_report)
                    status_data["results"] = {
                        "loads": df.to_dict(orient="records"),
                        "html_report_path": str(html_report)
                    }
                except Exception as e:
                    status_data["error"] = f"Failed to load nozzle CSV: {e}"
                    
    return jsonify(status_data)

@app.route("/api/task/<task_id>/logs")
def task_logs(task_id):
    """Retrieve full execution log content for a given task."""
    task = active_tasks.get(task_id)
    if not task:
        # Check if the log file exists on disk for historical task
        log_path = RUNS_DIR / task_id / "output.log"
        if log_path.exists():
            try:
                content = log_path.read_text(encoding="utf-8", errors="ignore")
                return jsonify({"logs": content})
            except Exception as e:
                return jsonify({"error": f"Failed to read log file: {str(e)}"}), 500
        return jsonify({"error": "Task not found"}), 404
        
    log_path = Path(task["log_file"])
    if not log_path.exists():
        return jsonify({"logs": ""})
        
    try:
        content = log_path.read_text(encoding="utf-8", errors="ignore")
        return jsonify({"logs": content})
    except Exception as e:
        return jsonify({"error": f"Failed to read log file: {str(e)}"}), 500

@app.route("/api/cad-validator/run", methods=["POST"])
def run_cad_validator():
    """Trigger the STEP-vs-DXF CAD Validator."""
    data = request.json or {}
    step = data.get("step")
    drawing = data.get("drawing")
    scale = data.get("scale", 1.0)
    rotations = data.get("rotations", {})
    
    if not step or not drawing:
        return jsonify({"error": "Missing 'step' or 'drawing' path parameter"}), 400
        
    task_id = f"cad_val_{int(time.time())}"
    task_dir = RUNS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    log_file = task_dir / "output.log"
    
    # Run the cad_runner.py helper subprocess
    cmd = [
        sys.executable,
        str(Path(__file__).parent / "cad_runner.py"),
        "--step", step,
        "--drawing", drawing,
        "--scale", str(scale),
        "--rotations", json.dumps(rotations),
        "--outdir", str(task_dir)
    ]
    
    try:
        log_handle = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(WORKSPACE_DIR)
        )
        
        active_tasks[task_id] = {
            "status": "running",
            "type": "cad-validator",
            "output_dir": task_dir,
            "log_file": log_file,
            "process": proc,
            "args": data
        }
        
        return jsonify({"task_id": task_id, "status": "running"})
    except Exception as e:
        return jsonify({"error": f"Failed to spawn validation subprocess: {e}"}), 500

@app.route("/api/dimension-checker/run", methods=["POST"])
def run_dimension_checker():
    """Trigger the DXF Dimension Checker."""
    data = request.json or {}
    drawing = data.get("drawing")
    
    if not drawing:
        return jsonify({"error": "Missing 'drawing' path parameter"}), 400
        
    task_id = f"dim_chk_{int(time.time())}"
    task_dir = RUNS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    log_file = task_dir / "output.log"
    plot_path = task_dir / "dimension_validation.png"
    
    # Run modified Dimension_Checker.py
    cmd = [
        sys.executable,
        str(WORKSPACE_DIR / "Asquareic_Dimension_validator" / "Dimension_Checker.py"),
        drawing,
        "--save-plot", str(plot_path)
    ]
    
    try:
        log_handle = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(WORKSPACE_DIR)
        )
        
        active_tasks[task_id] = {
            "status": "running",
            "type": "dimension-checker",
            "output_dir": task_dir,
            "log_file": log_file,
            "process": proc,
            "args": data
        }
        
        return jsonify({"task_id": task_id, "status": "running"})
    except Exception as e:
        return jsonify({"error": f"Failed to spawn dimension checker subprocess: {e}"}), 500

@app.route("/api/notes-checker/run", methods=["POST"])
def run_notes_checker():
    """Trigger the Notes Checker Pipeline & Validation."""
    data = request.json or {}
    drawing = data.get("drawing")
    docx = data.get("docx")
    
    if not drawing or not docx:
        return jsonify({"error": "Missing 'drawing' or 'docx' path parameter"}), 400
        
    task_id = f"notes_chk_{int(time.time())}"
    task_dir = RUNS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    log_file = task_dir / "output.log"
    
    # We need to run two scripts sequentially.
    # We will spawn a shell/bat commands script or run them inside a thread that handles execution.
    def execute_notes_pipeline():
        log_handle = open(log_file, "w", encoding="utf-8")
        try:
            # 1. Run extraction pipeline
            log_handle.write("--- STEP 1: Running Text Extraction Pipeline ---\n")
            log_handle.flush()
            cmd1 = [
                sys.executable,
                str(WORKSPACE_DIR / "Asquareic_Notes_checker" / "design_pipeline.py"),
                drawing,
                "--outdir", str(task_dir)
            ]
            p1 = subprocess.run(cmd1, stdout=log_handle, stderr=subprocess.STDOUT, text=True, cwd=str(WORKSPACE_DIR))
            
            if p1.returncode != 0:
                log_handle.write(f"\n[FAIL] Extraction script exited with error code {p1.returncode}\n")
                active_tasks[task_id]["status"] = "failed"
                active_tasks[task_id]["error"] = f"Extraction script failed (code {p1.returncode})"
                return
                
            # 2. Run validator against docx
            log_handle.write("\n--- STEP 2: Running DOCX Validation Crosscheck ---\n")
            log_handle.flush()
            cmd2 = [
                sys.executable,
                str(WORKSPACE_DIR / "Asquareic_Notes_checker" / "validate_design_data.py"),
                "--docx", docx,
                "--csv", str(task_dir / "design_data.csv"),
                "--outdir", str(task_dir)
            ]
            p2 = subprocess.run(cmd2, stdout=log_handle, stderr=subprocess.STDOUT, text=True, cwd=str(WORKSPACE_DIR))
            
            # Note: validate_design_data.py exits with code 1 if there are any failures,
            # but that is a validation failure (which is successful program execution), not a crash.
            # So we treat it as completed.
            active_tasks[task_id]["status"] = "completed"
            
        except Exception as e:
            log_handle.write(f"\n[CRASH] Subprocess pipeline manager error: {e}\n")
            active_tasks[task_id]["status"] = "failed"
            active_tasks[task_id]["error"] = str(e)
        finally:
            log_handle.close()
            
    try:
        active_tasks[task_id] = {
            "status": "running",
            "type": "notes-checker",
            "output_dir": task_dir,
            "log_file": log_file,
            "process": None, # managed in thread
            "args": data
        }
        
        # Start execution thread
        t = threading.Thread(target=execute_notes_pipeline)
        t.daemon = True
        t.start()
        
        return jsonify({"task_id": task_id, "status": "running"})
    except Exception as e:
        return jsonify({"error": f"Failed to spawn notes checker thread: {e}"}), 500

@app.route("/api/nozzle-validator/run", methods=["POST"])
def run_nozzle_validator():
    """Trigger the Piping Nozzle Load Validator."""
    data = request.json or {}
    drawing = data.get("drawing")
    allowables = data.get("allowables")
    project = data.get("project", "Pressure Vessel Nozzle Validation")
    temp = data.get("temp", 58.0)
    derating = data.get("derating", 1.0)
    ref_doc = data.get("ref_doc", "AGES-SP-06-001 Rev.1 Table A2-2")
    
    if not drawing or not allowables:
        return jsonify({"error": "Missing 'drawing' or 'allowables' CSV path"}), 400
        
    task_id = f"nozzle_val_{int(time.time())}"
    task_dir = RUNS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    log_file = task_dir / "output.log"
    
    # nozzle_pipeline.py args
    cmd = [
        sys.executable,
        str(WORKSPACE_DIR / "Asquareic_Nozzle_Load" / "nozzle_pipeline.py"),
        drawing,
        "--allowables", allowables,
        "--outdir", str(task_dir),
        "--out", "nozzle_validation_report.html",
        "--project", project,
        "--temp", str(temp),
        "--deratingfactor", str(derating),
        "--ref-doc", ref_doc
    ]
    
    try:
        log_handle = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(WORKSPACE_DIR)
        )
        
        active_tasks[task_id] = {
            "status": "running",
            "type": "nozzle-validator",
            "output_dir": task_dir,
            "log_file": log_file,
            "process": proc,
            "args": data
        }
        
        return jsonify({"task_id": task_id, "status": "running"})
    except Exception as e:
        return jsonify({"error": f"Failed to spawn nozzle validator subprocess: {e}"}), 500

@app.route("/api/runs/history")
def runs_history():
    """Retrieve list of completed/history runs from disk directory."""
    runs = []
    for d in RUNS_DIR.glob("*/"):
        if not d.is_dir():
            continue
            
        task_id = d.name
        # Match type from task_id prefix
        task_type = "unknown"
        if task_id.startswith("cad_val_"):
            task_type = "CAD Validator"
        elif task_id.startswith("dim_chk_"):
            task_type = "Dimension Checker"
        elif task_id.startswith("notes_chk_"):
            task_type = "Notes Checker"
        elif task_id.startswith("nozzle_val_"):
            task_type = "Nozzle Validator"
            
        timestamp_epoch = int(task_id.split("_")[-1]) if "_" in task_id else d.stat().st_mtime
        dt = datetime.fromtimestamp(timestamp_epoch)
        formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S")
        
        # Check files in task dir
        status = "completed"
        error = None
        
        if task_type == "CAD Validator":
            status_file = d / "cad_runner_status.json"
            if status_file.exists():
                try:
                    s_data = json.loads(status_file.read_text())
                    status = s_data.get("status", "completed")
                    error = s_data.get("error", None)
                except Exception:
                    pass
        elif task_type == "Notes Checker":
            # For notes checker, if log file exists but validation_report.csv doesn't, it failed
            if not (d / "validation_report.csv").exists() and (d / "output.log").exists():
                status = "failed"
                error = "Report output missing"
        elif task_type == "Nozzle Validator":
            if not (d / "nozzle_loads_table.csv").exists():
                status = "failed"
                
        runs.append({
            "task_id": task_id,
            "type": task_type,
            "output_dir": str(d),
            "date": formatted_date,
            "status": status,
            "error": error
        })
        
    # Sort runs by date descending
    runs.sort(key=lambda r: r["task_id"], reverse=True)
    return jsonify(runs)

# ---------------------------------------------------------------------------
# Main Launcher
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Check for Flask port collisions, run locally on 5000 (bind localhost only)
    print("====================================================")
    print("  Asquareic Engineering CAD Unified Dashboard")
    print("  Local web server starting on http://127.0.0.1:5000")
    print("====================================================")
    app.run(host="127.0.0.1", port=5000, debug=True)
