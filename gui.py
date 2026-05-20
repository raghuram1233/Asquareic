"""
Tkinter GUI for STEP-vs-DWG/DXF validation.

Run:
    python gui.py

The GUI accepts a STEP model and a DWG/DXF sheet drawing, splits the drawing
into the six standard views, runs the existing validation pipeline, and lets
the user inspect the per-view comparison images one at a time.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from tkinter import (
    BOTH,
    DISABLED,
    END,
    HORIZONTAL,
    LEFT,
    NORMAL,
    RIGHT,
    TOP,
    StringVar,
    Tk,
    filedialog,
    messagebox,
    ttk,
)
from tkinter import PhotoImage

from split_dxf_views import split_dxf_views


VIEW_ORDER = ("front", "back", "top", "bottom", "left", "right")
IMAGE_MODES = {
    "Diff": "diff",
    "Overlay": "overlay",
    "Model": "model_cropped",
    "Drawing": "drawing_cropped",
}


class QueueLogHandler(logging.Handler):
    def __init__(self, messages: "queue.Queue[tuple[str, str]]"):
        super().__init__()
        self.messages = messages

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.put(("log", self.format(record)))


def find_oda_converter() -> Path | None:
    exe = shutil.which("ODAFileConverter")
    if exe:
        return Path(exe)

    candidates = sorted(
        Path("C:/Program Files/ODA").glob("ODAFileConverter*/ODAFileConverter.exe"),
        reverse=True,
    )
    return candidates[0] if candidates else None


def convert_dwg_to_dxf(dwg_path: Path, output_dir: Path, log: logging.Logger) -> Path:
    converter = find_oda_converter()
    if converter is None:
        sibling_dxf = dwg_path.with_suffix(".dxf")
        if sibling_dxf.exists():
            log.warning("ODA converter not found; using sibling DXF: %s", sibling_dxf)
            return sibling_dxf
        raise RuntimeError(
            "DWG conversion needs ODA File Converter, or a DXF with the same name "
            "next to the DWG."
        )

    work_in = output_dir / "_dwg_input"
    work_out = output_dir / "_dwg_converted"
    work_in.mkdir(parents=True, exist_ok=True)
    work_out.mkdir(parents=True, exist_ok=True)

    staged_dwg = work_in / dwg_path.name
    shutil.copy2(dwg_path, staged_dwg)

    cmd = [
        str(converter),
        str(work_in),
        str(work_out),
        "ACAD2018",
        "DXF",
        "0",
        "1",
        "*.dwg",
    ]
    log.info("Converting DWG to DXF with ODA File Converter...")
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.stdout.strip():
        log.info(completed.stdout.strip())
    if completed.stderr.strip():
        log.warning(completed.stderr.strip())
    if completed.returncode != 0:
        raise RuntimeError(f"ODA File Converter failed with code {completed.returncode}")

    matches = list(work_out.rglob(f"{dwg_path.stem}.dxf"))
    if not matches:
        matches = list(work_out.rglob("*.dxf"))
    if not matches:
        raise RuntimeError("DWG conversion completed but no DXF output was found.")
    return matches[0]


def prepare_drawing_views(drawing_path: Path, output_dir: Path, log: logging.Logger):
    from cad_validator.geometry_store import ViewName

    split_dir = output_dir / "split_views"
    suffix = drawing_path.suffix.lower()

    if drawing_path.is_dir():
        source_dir = drawing_path
    else:
        if suffix == ".dwg":
            drawing_path = convert_dwg_to_dxf(drawing_path, output_dir, log)
        elif suffix != ".dxf":
            raise ValueError("Please select a DWG, DXF, or folder containing per-view DXFs.")

        log.info("Splitting drawing into clean six-view DXFs...")
        split_dxf_views(drawing_path, split_dir)
        source_dir = split_dir

    mapping = {}
    for view in ViewName:
        view_file = source_dir / f"{view.value}_view.dxf"
        if not view_file.exists():
            raise FileNotFoundError(f"Missing view DXF: {view_file}")
        mapping[view] = str(view_file)
    return mapping


class CadDiffGui:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("CAD View Difference Inspector")
        self.root.geometry("1180x820")
        self.root.minsize(980, 700)

        self.messages: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.current_view_index = 0
        self.current_image_mode = StringVar(value="Diff")
        self.step_path = StringVar(value=str(Path("Data/Model.STEP")))
        self.drawing_path = StringVar(value=str(Path("Data/drawing.dwg")))
        self.output_dir = StringVar(value=str(Path("gui_results")))
        self.status = StringVar(value="Choose files and run comparison.")
        self.score = StringVar(value="")
        self.view_title = StringVar(value="No results yet")
        self.report_data: dict = {}
        self.image_cache: PhotoImage | None = None
        self.result_dir: Path | None = None

        self._build_style()
        self._build_layout()
        self.root.after(120, self._poll_messages)

    def _build_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#10151f")
        style.configure("Panel.TFrame", background="#151d2b")
        style.configure("TLabel", background="#10151f", foreground="#e8edf5")
        style.configure("Muted.TLabel", background="#10151f", foreground="#9aa7b8")
        style.configure("Panel.TLabel", background="#151d2b", foreground="#e8edf5")
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("View.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 6))
        style.configure("Accent.TButton", background="#1f8a70", foreground="white")
        style.configure("TRadiobutton", background="#151d2b", foreground="#e8edf5")
        style.map("TRadiobutton", background=[("active", "#1b2638")])

    def _build_layout(self) -> None:
        main = ttk.Frame(self.root, padding=18)
        main.pack(fill=BOTH, expand=True)

        ttk.Label(main, text="CAD View Difference Inspector", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            main,
            text="Select a STEP model and a DWG/DXF drawing sheet, then inspect each view's difference image.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 14))

        inputs = ttk.Frame(main, style="Panel.TFrame", padding=14)
        inputs.pack(fill="x", pady=(0, 14))

        self._file_row(inputs, "STEP model", self.step_path, self._browse_step, 0)
        self._file_row(inputs, "DWG / DXF drawing", self.drawing_path, self._browse_drawing, 1)
        self._file_row(inputs, "Output folder", self.output_dir, self._browse_output, 2)

        actions = ttk.Frame(inputs, style="Panel.TFrame")
        actions.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        self.run_button = ttk.Button(actions, text="Run Comparison", style="Accent.TButton", command=self._run)
        self.run_button.pack(side=LEFT)
        self.open_button = ttk.Button(actions, text="Open Report Folder", command=self._open_report, state=DISABLED)
        self.open_button.pack(side=LEFT, padx=(8, 0))
        self.progress = ttk.Progressbar(actions, mode="indeterminate", orient=HORIZONTAL, length=220)
        self.progress.pack(side=RIGHT, padx=(8, 0))

        ttk.Label(main, textvariable=self.status, style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        viewer = ttk.Frame(main, style="Panel.TFrame", padding=12)
        viewer.pack(fill=BOTH, expand=True)

        top_bar = ttk.Frame(viewer, style="Panel.TFrame")
        top_bar.pack(fill="x")
        ttk.Label(top_bar, textvariable=self.view_title, style="View.TLabel").pack(side=LEFT)
        ttk.Label(top_bar, textvariable=self.score, style="Panel.TLabel").pack(side=RIGHT)

        mode_bar = ttk.Frame(viewer, style="Panel.TFrame")
        mode_bar.pack(fill="x", pady=(8, 10))
        for label in IMAGE_MODES:
            ttk.Radiobutton(
                mode_bar,
                text=label,
                variable=self.current_image_mode,
                value=label,
                command=self._refresh_image,
            ).pack(side=LEFT, padx=(0, 14))

        self.image_label = ttk.Label(viewer, text="Run a comparison to see the first view.", style="Panel.TLabel")
        self.image_label.pack(fill=BOTH, expand=True)

        nav = ttk.Frame(viewer, style="Panel.TFrame")
        nav.pack(fill="x", pady=(10, 0))
        self.prev_button = ttk.Button(nav, text="Previous View", command=self._previous_view, state=DISABLED)
        self.prev_button.pack(side=LEFT)
        self.next_button = ttk.Button(nav, text="Next View", command=self._next_view, state=DISABLED)
        self.next_button.pack(side=LEFT, padx=(8, 0))

        log_frame = ttk.Frame(main, padding=(0, 12, 0, 0))
        log_frame.pack(fill="x")
        self.log_box = ttk.Treeview(log_frame, columns=("message",), show="", height=5)
        self.log_box.pack(fill="x")

    def _file_row(self, parent, label: str, variable: StringVar, command, row: int) -> None:
        ttk.Label(parent, text=label, style="Panel.TLabel").grid(row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=10, pady=4)
        ttk.Button(parent, text="Browse", command=command).grid(row=row, column=2, sticky="e", pady=4)
        parent.columnconfigure(1, weight=1)

    def _browse_step(self) -> None:
        path = filedialog.askopenfilename(
            title="Select STEP file",
            filetypes=[("STEP files", "*.step *.stp"), ("All files", "*.*")],
        )
        if path:
            self.step_path.set(path)

    def _browse_drawing(self) -> None:
        path = filedialog.askopenfilename(
            title="Select DWG or DXF drawing",
            filetypes=[("CAD drawings", "*.dwg *.dxf"), ("All files", "*.*")],
        )
        if path:
            self.drawing_path.set(path)

    def _browse_output(self) -> None:
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_dir.set(path)

    def _run(self) -> None:
        step = Path(self.step_path.get()).expanduser()
        drawing = Path(self.drawing_path.get()).expanduser()
        output = Path(self.output_dir.get()).expanduser()

        if not step.exists():
            messagebox.showerror("Missing STEP", f"STEP file not found:\n{step}")
            return
        if not drawing.exists():
            messagebox.showerror("Missing drawing", f"Drawing file not found:\n{drawing}")
            return

        self._set_running(True)
        self._clear_log()
        self.status.set("Running comparison...")
        worker = threading.Thread(target=self._run_worker, args=(step, drawing, output), daemon=True)
        worker.start()

    def _run_worker(self, step: Path, drawing: Path, output: Path) -> None:
        logger = logging.getLogger()
        handler = QueueLogHandler(self.messages)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        try:
            from cad_validator.geometry_store import ViewName
            from cad_validator.pipeline import Pipeline, PipelineConfig

            run_dir = output / datetime.now().strftime("run_%Y%m%d_%H%M%S")
            run_dir.mkdir(parents=True, exist_ok=True)
            drawing_views = prepare_drawing_views(drawing, run_dir, logging.getLogger(__name__))

            config = PipelineConfig(views=list(ViewName), output_dir=str(run_dir))
            report = Pipeline(config).run(str(step), drawing_views)
            self.messages.put(("done", {"run_dir": run_dir, "passed": report.passed}))
        except Exception as exc:
            self.messages.put(("error", str(exc)))
        finally:
            logger.removeHandler(handler)

    def _poll_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                    self.status.set(str(payload))
                elif kind == "done":
                    self._set_running(False)
                    self.result_dir = Path(payload["run_dir"])
                    self._load_results()
                    self.open_button.configure(state=NORMAL)
                    status = "passed" if payload["passed"] else "completed with differences"
                    self.status.set(f"Comparison {status}. Results: {self.result_dir}")
                elif kind == "error":
                    self._set_running(False)
                    self.status.set("Comparison failed.")
                    messagebox.showerror("Comparison failed", str(payload))
        except queue.Empty:
            pass
        self.root.after(120, self._poll_messages)

    def _set_running(self, running: bool) -> None:
        self.run_button.configure(state=DISABLED if running else NORMAL)
        if running:
            self.progress.start(12)
            self.prev_button.configure(state=DISABLED)
            self.next_button.configure(state=DISABLED)
        else:
            self.progress.stop()

    def _load_results(self) -> None:
        if self.result_dir is None:
            return
        report_path = self.result_dir / "validation_report.json"
        if report_path.exists():
            self.report_data = json.loads(report_path.read_text())
        else:
            self.report_data = {}
        self.current_view_index = 0
        self.prev_button.configure(state=NORMAL)
        self.next_button.configure(state=NORMAL)
        self._refresh_image()

    def _refresh_image(self) -> None:
        if self.result_dir is None:
            return
        view = VIEW_ORDER[self.current_view_index]
        mode = IMAGE_MODES[self.current_image_mode.get()]
        image_path = self.result_dir / "image_comparison" / f"img_{view}_{mode}.png"

        self.view_title.set(f"{view.upper()} view")
        view_data = self.report_data.get("views", {}).get(view, {})
        image_data = view_data.get("image_comparison", {})
        if image_data:
            self.score.set(
                "Overlap {overlap:.1%}   Model {model:.1%}   Drawing {drawing:.1%}".format(
                    overlap=image_data.get("overlap_score", 0.0),
                    model=image_data.get("model_coverage", 0.0),
                    drawing=image_data.get("drawing_coverage", 0.0),
                )
            )
        else:
            self.score.set("")

        if not image_path.exists():
            self.image_cache = None
            self.image_label.configure(text=f"Missing image:\n{image_path}", image="")
            return

        image = PhotoImage(file=str(image_path))
        max_w = max(self.image_label.winfo_width(), 900)
        max_h = max(self.image_label.winfo_height(), 520)
        factor = max(1, math.ceil(max(image.width() / max_w, image.height() / max_h)))
        if factor > 1:
            image = image.subsample(factor, factor)
        self.image_cache = image
        self.image_label.configure(image=self.image_cache, text="")

    def _previous_view(self) -> None:
        self.current_view_index = (self.current_view_index - 1) % len(VIEW_ORDER)
        self._refresh_image()

    def _next_view(self) -> None:
        self.current_view_index = (self.current_view_index + 1) % len(VIEW_ORDER)
        self._refresh_image()

    def _open_report(self) -> None:
        if self.result_dir and self.result_dir.exists():
            os.startfile(self.result_dir)

    def _append_log(self, message: str) -> None:
        self.log_box.insert("", END, values=(message,))
        children = self.log_box.get_children()
        if len(children) > 80:
            self.log_box.delete(children[0])
        if children:
            self.log_box.see(children[-1])

    def _clear_log(self) -> None:
        for item in self.log_box.get_children():
            self.log_box.delete(item)


def main() -> None:
    root = Tk()
    CadDiffGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
