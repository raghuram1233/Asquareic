# Asquareic Engineering CAD Checker Suite & Dashboard

A consolidated suite of automated CAD drawing check, validation, and engineering reporting tools. This workspace groups four separate mechanical calculation and drafting checker components and integrates them under a unified, dark-themed local web dashboard.

---

## 📂 Repository Layout

```text
c:/TRR/TRR/Asquareic_combined/
├── README.md                           # Master documentation (this file)
│
├── Asquareic_Unified_Dashboard/        # Combined local web application UI
│   ├── app.py                          # Flask backend web API server
│   ├── cad_runner.py                   # CAD validator execution script
│   ├── run.bat                         # Double-clickable server launcher
│   ├── requirements.txt                # Python dependencies list
│   ├── templates/                      # HTML templates
│   │   └── index.html                  # Dashboard web UI layout
│   └── static/                         # Static UI stylesheets & scripts
│       ├── css/style.css               # Vanilla CSS styling
│       └── js/main.js                  # Frontend Javascript logic
│
├── Asquareic_Cad_Validator/            # 3D STEP vs 2D DXF view validator
│   ├── main.py                         # CLI script entry point
│   ├── gui.py                          # Tkinter GUI script
│   └── cad_validator/                  # Core geometry parsing library
│
├── Asquareic_Dimension_validator/      # DXF text-to-geometry validator
│   └── Dimension_Checker.py            # Dimension validation pipeline
│
├── Asquareic_Notes_checker/            # AutoCAD notes vs PV Elite docx report
│   ├── design_pipeline.py              # Text tables extractor
│   └── validate_design_data.py         # Expected values validator
│
└── Asquareic_Nozzle_Load/              # Drawing nozzle loads vs allowables CSV
    ├── nozzle_pipeline.py              # Table extraction & load check pipeline
    └── nozzle_allowables.csv           # Allowable nozzle loads database
```

---

## 🚀 Getting Started (Unified Dashboard)

The unified web dashboard provides a graphical, interactive interface to configure parameters, browse host directories, run verification pipelines, and view reports side-by-side.

### Quick Start
1. Double-click the launcher script inside the dashboard folder:
   [Asquareic_Unified_Dashboard/run.bat](file:///c:/TRR/TRR/Asquareic_combined/Asquareic_Unified_Dashboard/run.bat)
2. This script will automatically verify python dependencies, start the backend Flask server at `http://127.0.0.1:5000`, and launch your default web browser to open the dashboard.

---

## 🛠️ Environment Prerequisites

To run all four pipelines and the web server, configure the host environment with the following dependencies.

### 1. Python Libraries
Install all requirements from the dashboard directory using `pip`:
```powershell
pip install -r Asquareic_Unified_Dashboard/requirements.txt
```
Key packages include:
* `flask` (Dashboard web server)
* `ezdxf` (DXF vector drawing parsing)
* `python-docx` (Word document PV Elite parsing)
* `pandas` & `openpyxl` (Tabular data check and Excel reporting)
* `opencv-python` & `matplotlib` (Raster image processing and overlap overlay grids)
* `shapely` & `scipy` (Spatial geometric indices and coordinates clustering)

### 2. OpenCascade (OCP) Bindings
Required by the **CAD Validator** to process 3D STEP solid models.
```powershell
pip install cadquery-ocp>=7.7
```

### 3. ODA File Converter (DWG → DXF)
AutoCAD `.dwg` files must be parsed via a vector engine. If `ezdxf` cannot load a drawing directly (e.g., if it is in a newer or compressed AutoCAD format), the pipelines automatically fall back to using the ODA File Converter CLI.
* Download and run the official installer: [ODA File Converter](https://www.opendesign.com/guestfiles/oda_file_converter).
* **PATH Setup**: Make sure `ODAFileConverter.exe` is added to your system environment variables PATH.

---

## ⚙️ Core Tool Descriptions

### 1. CAD Validator
* **Goal**: Verifies that a physical 3D STEP model matches the layout drawn on a 2D DXF engineering drawing sheet.
* **Pipeline**: Projects the 3D model into 6 orthographic views (Front, Back, Top, Bottom, Left, Right), performs Hidden Line Removal (HLR), extracts lines, and matches them to drawings using HSL image overlay comparator and geometry search.

### 2. Dimension Checker
* **Goal**: Scrapes text annotations from drawing sheets and ensures that the printed numeric dimensions match the actual CAD geometry (lines, arcs, circles).
* **Pipeline**: Clusters geometries, infers drafting scales, overlays pass/fail badges on a Matplotlib figure, and highlights discrepancies.

### 3. Notes Checker
* **Goal**: Performs quality control check to guarantee that general notes and details tables printed on AutoCAD drawing sheets match the approved PV Elite mechanical calculation report.
* **Pipeline**: Scrapes tables coordinates, clusters text positional indices to align keys with values, parses DOCX properties, and prints a cross-check checklist.

### 4. Nozzle Load Validator
* **Goal**: Extracts piping nozzle load forces (FL, FC, FA) and moments (ML, MC, MT) from drawings and validates them against allowable limit tables.
* **Pipeline**: Reconstructs nozzle cells, checks allowables from `nozzle_allowables.csv` database, applies temperature de-rating factors, and compiles HTML reports.
