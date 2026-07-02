document.addEventListener("DOMContentLoaded", () => {
  // Initialize subsystems
  initTheme();
  initTabs();
  initDiagnostics();
  initFileBrowser();
  initTasks();

  // Initialize Zoom Viewports
  initPanZoom("cad-zoom-viewport", "cad-zoom-content", "cad-zoom-in", "cad-zoom-out", "cad-zoom-reset");
  initPanZoom("dim-zoom-viewport", "dim-zoom-content", "dim-zoom-in", "dim-zoom-out", "dim-zoom-reset");
});

// =============================================================================
// Theme Toggle (Dark / Light)
// =============================================================================
function initTheme() {
  const root = document.documentElement;
  const toggleBtn = document.getElementById("btn-theme-toggle");
  const themeIcon = document.getElementById("theme-icon");

  // Restore saved preference (default: dark)
  const saved = localStorage.getItem("asquareic-theme") || "dark";
  applyTheme(saved);

  toggleBtn.addEventListener("click", () => {
    const current = root.getAttribute("data-theme") === "light" ? "light" : "dark";
    const next = current === "dark" ? "light" : "dark";
    applyTheme(next);
    localStorage.setItem("asquareic-theme", next);
  });

  function applyTheme(theme) {
    if (theme === "light") {
      root.setAttribute("data-theme", "light");
      themeIcon.textContent = "dark_mode";
      toggleBtn.title = "Switch to dark theme";
    } else {
      root.removeAttribute("data-theme");
      themeIcon.textContent = "light_mode";
      toggleBtn.title = "Switch to light theme";
    }
  }
}



// =============================================================================
// Tab Switching Manager
// =============================================================================
const tabDetails = {
  "tab-overview": {
    title: "Dashboard Overview",
    desc: "General health diagnostics, past execution runs, and workspace metrics."
  },
  "tab-cad-validator": {
    title: "CAD STEP vs DXF Validator",
    desc: "Compare 3D STEP solid models against standard orthographic DXF engineering drawings."
  },
  "tab-dimension-checker": {
    title: "2D DXF Dimension Checker",
    desc: "Scan DXF/DWG file entities to verify drafting dimensions against physical CAD geometry."
  },
  "tab-notes-checker": {
    title: "Engineering Notes Checker",
    desc: "Validate textual AutoCAD details tables against expected PV Elite mechanical outputs."
  },
  "tab-nozzle-validator": {
    title: "Piping Nozzle Load Validator",
    desc: "Cross-check piping nozzle loads table in drawings against standard allowable limit tables."
  },
  "tab-logs": {
    title: "System Log Terminal",
    desc: "Captures stdout and stderr streams of validator pipeline subprocess runs."
  }
};

function initTabs() {
  const navItems = document.querySelectorAll(".nav-item");
  const tabPanels = document.querySelectorAll(".tab-panel");
  const pageTitle = document.getElementById("page-title");
  const pageDescription = document.getElementById("page-description");

  navItems.forEach(item => {
    item.addEventListener("click", () => {
      const targetTab = item.getAttribute("data-tab");
      
      // Update sidebar nav items
      navItems.forEach(btn => btn.classList.remove("active"));
      item.classList.add("active");

      // Update panels view
      tabPanels.forEach(panel => panel.classList.remove("active"));
      document.getElementById(targetTab).classList.add("active");

      // Update page headers text
      if (tabDetails[targetTab]) {
        pageTitle.textContent = tabDetails[targetTab].title;
        pageDescription.textContent = tabDetails[targetTab].desc;
      }
    });
  });
}

// =============================================================================
// Diagnostics & Runs History
// =============================================================================
function initDiagnostics() {
  const refreshBtn = document.getElementById("btn-refresh-status");
  
  if (refreshBtn) {
    refreshBtn.addEventListener("click", loadDiagnostics);
  }
  
  loadDiagnostics();
}

async function loadDiagnostics() {
  const diagList = document.getElementById("diagnostics-list");
  const odaPathVal = document.getElementById("oda-path-value");
  const workspacePath = document.getElementById("workspace-path-text");
  
  diagList.innerHTML = '<div class="loading-spinner"></div>';
  
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    
    // Render dependencies list
    diagList.innerHTML = "";
    Object.entries(data.dependencies).forEach(([lib, ok]) => {
      const item = document.createElement("div");
      item.className = "diag-item";
      
      const label = document.createElement("span");
      label.className = "diag-label";
      label.textContent = lib;
      
      const status = document.createElement("span");
      status.className = `diag-status ${ok ? 'ok' : 'missing'}`;
      status.textContent = ok ? "Installed" : "Missing";
      
      item.appendChild(label);
      item.appendChild(status);
      diagList.appendChild(item);
    });
    
    odaPathVal.textContent = data.oda_converter_path;
    workspacePath.textContent = data.workspace;
    
    loadRunsHistory();
  } catch (err) {
    console.error("Failed to load status:", err);
    diagList.innerHTML = `<div class="terminal-line stderr-line">[ERROR] Diagnostics check failed: ${err.message}</div>`;
  }
}

async function loadRunsHistory() {
  const tableBody = document.querySelector("#runs-history-table tbody");
  tableBody.innerHTML = '<tr><td colspan="5" class="empty-row">Loading run history...</td></tr>';
  
  try {
    const res = await fetch("/api/runs/history");
    const runs = await res.json();
    
    if (runs.length === 0) {
      tableBody.innerHTML = '<tr><td colspan="5" class="empty-row">No runs executed yet.</td></tr>';
      return;
    }
    
    tableBody.innerHTML = "";
    runs.forEach(run => {
      const tr = document.createElement("tr");
      
      const tdDate = document.createElement("td");
      tdDate.textContent = run.date;
      
      const tdType = document.createElement("td");
      tdType.innerHTML = `<strong>${run.type}</strong>`;
      
      const tdId = document.createElement("td");
      tdId.className = "code-text";
      tdId.textContent = run.task_id;
      
      const tdStatus = document.createElement("td");
      const badge = document.createElement("span");
      badge.className = `status-badge ${run.status}`;
      badge.textContent = run.status;
      tdStatus.appendChild(badge);
      
      const tdActions = document.createElement("td");
      const viewBtn = document.createElement("button");
      viewBtn.className = "btn btn-secondary btn-small";
      viewBtn.textContent = "View Results";
      viewBtn.addEventListener("click", () => loadPastRunResults(run));
      tdActions.appendChild(viewBtn);
      
      tr.appendChild(tdDate);
      tr.appendChild(tdType);
      tr.appendChild(tdId);
      tr.appendChild(tdStatus);
      tr.appendChild(tdActions);
      
      tableBody.appendChild(tr);
    });
  } catch (err) {
    console.error("Failed to load history runs:", err);
    tableBody.innerHTML = '<tr><td colspan="5" class="empty-row stderr-line">Failed to fetch run history.</td></tr>';
  }
}

// =============================================================================
// Host Directory File Browser Modal
// =============================================================================
let activeBrowseTargetInput = null;
let currentBrowserDirectory = "";
let selectedItemPath = null;

function initFileBrowser() {
  const modal = document.getElementById("file-browser-modal");
  const closeBtn = document.getElementById("btn-close-browser");
  const backBtn = document.getElementById("btn-browser-back");
  const selectBtn = document.getElementById("btn-browser-select");
  const browseButtons = document.querySelectorAll(".btn-browse");
  
  browseButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      const targetId = btn.getAttribute("data-target");
      activeBrowseTargetInput = document.getElementById(targetId);
      
      // Open modal
      modal.classList.add("active");
      const currentVal = activeBrowseTargetInput.value.trim();
      fetchBrowse(currentVal || "");
    });
  });
  
  closeBtn.addEventListener("click", () => {
    modal.classList.remove("active");
  });
  
  backBtn.addEventListener("click", () => {
    const parentPath = backBtn.getAttribute("data-parent");
    if (parentPath) {
      fetchBrowse(parentPath);
    }
  });
  
  selectBtn.addEventListener("click", () => {
    if (selectedItemPath && activeBrowseTargetInput) {
      activeBrowseTargetInput.value = selectedItemPath;
      modal.classList.remove("active");
    }
  });
}

async function fetchBrowse(path) {
  const container = document.getElementById("browser-listing-container");
  const currentPathInput = document.getElementById("browser-current-path");
  const selectBtn = document.getElementById("btn-browser-select");
  const backBtn = document.getElementById("btn-browser-back");
  const selectedDisplay = document.getElementById("browser-selected-display");
  
  container.innerHTML = '<div class="loading-spinner"></div>';
  selectBtn.disabled = true;
  selectedItemPath = null;
  selectedDisplay.textContent = "None";
  
  try {
    const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
    if (!res.ok) {
      // If error occurs, fallback to default browse
      if (path) {
        fetchBrowse("");
        return;
      }
      throw new Error("Directory browsing error");
    }
    
    const data = await res.json();
    currentBrowserDirectory = data.current_path;
    currentPathInput.value = data.current_path;
    
    // Update back button status
    if (data.parent_path) {
      backBtn.disabled = false;
      backBtn.setAttribute("data-parent", data.parent_path);
    } else {
      backBtn.disabled = true;
    }
    
    container.innerHTML = "";
    
    // Render Directories
    data.dirs.forEach(dir => {
      const item = document.createElement("div");
      item.className = "browser-item dir";
      item.innerHTML = `
        <span class="material-icons-outlined icon">folder</span>
        <span class="name">${dir.name}</span>
      `;
      
      item.addEventListener("click", () => {
        document.querySelectorAll(".browser-item").forEach(el => el.classList.remove("selected"));
        item.classList.add("selected");
        selectedItemPath = dir.path;
        selectedDisplay.textContent = dir.name;
        selectBtn.disabled = false;
      });
      
      item.addEventListener("dblclick", () => {
        fetchBrowse(dir.path);
      });
      
      container.appendChild(item);
    });
    
    // Render Files
    data.files.forEach(file => {
      const item = document.createElement("div");
      item.className = "browser-item file";
      
      // Pick file icon based on extension
      let iconName = "insert_drive_file";
      const ext = file.name.split(".").pop().toLowerCase();
      if (ext === "dwg" || ext === "dxf") iconName = "draw";
      else if (ext === "step" || ext === "stp") iconName = "layers";
      else if (ext === "docx") iconName = "description";
      else if (ext === "csv" || ext === "xlsx") iconName = "table_view";
      else if (ext === "png" || ext === "jpg") iconName = "image";
      
      // Format file size
      const kbSize = (file.size / 1024).toFixed(1);
      
      item.innerHTML = `
        <span class="material-icons-outlined icon">${iconName}</span>
        <span class="name">${file.name}</span>
        <span class="size">${kbSize} KB</span>
      `;
      
      item.addEventListener("click", () => {
        document.querySelectorAll(".browser-item").forEach(el => el.classList.remove("selected"));
        item.classList.add("selected");
        selectedItemPath = file.path;
        selectedDisplay.textContent = file.name;
        selectBtn.disabled = false;
      });
      
      container.appendChild(item);
    });
    
    if (data.dirs.length === 0 && data.files.length === 0) {
      container.innerHTML = '<div class="empty-row">This directory is empty or contains no matching files.</div>';
    }
  } catch (err) {
    console.error("Browse failed:", err);
    container.innerHTML = `<div class="terminal-line stderr-line">[ERROR] Directory browse failed: ${err.message}</div>`;
  }
}

// =============================================================================
// Reusable CSS Pan & Zoom Engine
// =============================================================================
function initPanZoom(viewportId, contentId, zoomInId, zoomOutId, resetId) {
  const viewport = document.getElementById(viewportId);
  const content = document.getElementById(contentId);
  const zIn = document.getElementById(zoomInId);
  const zOut = document.getElementById(zoomOutId);
  const zReset = document.getElementById(resetId);

  let scale = 1;
  let panX = 0;
  let panY = 0;
  let isPanning = false;
  let startX = 0;
  let startY = 0;

  function updateTransform() {
    content.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
  }

  // Scroll wheel zoom
  viewport.addEventListener("wheel", (e) => {
    e.preventDefault();
    const zoomFactor = 1.1;
    
    // Zoom center relative coordinates
    const rect = viewport.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseY = e.clientY - rect.top;

    // Relative offset of content origin before scaling
    const dx = mouseX - panX;
    const dy = mouseY - panY;

    // Apply scale change
    const oldScale = scale;
    if (e.deltaY < 0) {
      scale = Math.min(scale * zoomFactor, 25);
    } else {
      scale = Math.max(scale / zoomFactor, 0.15);
    }

    // Offset pan coordinates to keep mouse position anchored
    panX = mouseX - dx * (scale / oldScale);
    panY = mouseY - dy * (scale / oldScale);

    updateTransform();
  });

  // Click & Drag pan
  viewport.addEventListener("mousedown", (e) => {
    isPanning = true;
    startX = e.clientX - panX;
    startY = e.clientY - panY;
    viewport.style.cursor = "grabbing";
  });

  window.addEventListener("mousemove", (e) => {
    if (!isPanning) return;
    panX = e.clientX - startX;
    panY = e.clientY - startY;
    updateTransform();
  });

  window.addEventListener("mouseup", () => {
    isPanning = false;
    viewport.style.cursor = "grab";
  });

  // Buttons triggers
  zIn.addEventListener("click", () => {
    scale = Math.min(scale * 1.25, 25);
    updateTransform();
  });

  zOut.addEventListener("click", () => {
    scale = Math.max(scale / 1.25, 0.15);
    updateTransform();
  });

  zReset.addEventListener("click", () => {
    scale = 1;
    panX = 0;
    panY = 0;
    updateTransform();
  });
}

// =============================================================================
// Pipelines Execution Manager
// =============================================================================
// Global state of results payload cached
let cadResultsData = null;

function initTasks() {
  // CAD Validator Runner
  document.getElementById("btn-run-cad").addEventListener("click", () => {
    const step = document.getElementById("cad-step-input").value.trim();
    const drawing = document.getElementById("cad-drawing-input").value.trim();
    const scale = parseFloat(document.getElementById("cad-scale-input").value) || 1.0;
    
    const rotations = {
      "front": parseFloat(document.getElementById("rot-front").value) || 0.0,
      "back": parseFloat(document.getElementById("rot-back").value) || 0.0,
      "top": parseFloat(document.getElementById("rot-top").value) || 0.0,
      "bottom": parseFloat(document.getElementById("rot-bottom").value) || 0.0,
      "left": parseFloat(document.getElementById("rot-left").value) || 0.0,
      "right": parseFloat(document.getElementById("rot-right").value) || 0.0
    };
    
    startSubprocessTask("/api/cad-validator/run", { step, drawing, scale, rotations }, "cad-progress-box", "cad-stream-preview");
  });

  // Dimension Checker Runner
  document.getElementById("btn-run-dim").addEventListener("click", () => {
    const drawing = document.getElementById("dim-drawing-input").value.trim();
    startSubprocessTask("/api/dimension-checker/run", { drawing }, "dim-progress-box", "dim-stream-preview");
  });

  // Notes Checker Runner
  document.getElementById("btn-run-notes").addEventListener("click", () => {
    const drawing = document.getElementById("notes-drawing-input").value.trim();
    const docx = document.getElementById("notes-docx-input").value.trim();
    startSubprocessTask("/api/notes-checker/run", { drawing, docx }, "notes-progress-box", "notes-stream-preview");
  });

  // Nozzle Validator Runner
  document.getElementById("btn-run-nozzle").addEventListener("click", () => {
    const drawing = document.getElementById("nozzle-drawing-input").value.trim();
    const allowables = document.getElementById("nozzle-allowables-input").value.trim();
    const project = document.getElementById("nozzle-project").value.trim();
    const temp = parseFloat(document.getElementById("nozzle-temp").value) || 58.0;
    const derating = parseFloat(document.getElementById("nozzle-derating").value) || 1.0;
    const refDoc = document.getElementById("nozzle-refdoc").value.trim();
    
    startSubprocessTask("/api/nozzle-validator/run", { drawing, allowables, project, temp, derating, ref_doc: refDoc }, "nozzle-progress-box", "nozzle-stream-preview");
  });

  // Clear Terminal Button
  document.getElementById("btn-clear-terminal").addEventListener("click", () => {
    const terminal = document.getElementById("log-terminal-output");
    terminal.innerHTML = '<div class="terminal-line system-line">[SYSTEM] Dashboard log cleared.</div>';
  });

  // CAD selectors bindings
  document.querySelectorAll("#cad-view-selector button").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#cad-view-selector button").forEach(el => el.classList.remove("active"));
      btn.classList.add("active");
      renderCadImageResults();
    });
  });

  document.querySelectorAll("#cad-mode-selector button").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#cad-mode-selector button").forEach(el => el.classList.remove("active"));
      btn.classList.add("active");
      renderCadImageResults();
    });
  });
}

function appendToTerminal(text, isError = false) {
  const terminal = document.getElementById("log-terminal-output");
  const line = document.createElement("div");
  line.className = `terminal-line ${isError ? 'stderr-line' : 'stdout-line'}`;
  line.textContent = text;
  terminal.appendChild(line);
  terminal.scrollTop = terminal.scrollHeight;
}

// Subprocess task executor & log streaming poller
function startSubprocessTask(url, payload, progressBoxId, streamPreviewId) {
  const progressBox = document.getElementById(progressBoxId);
  const streamPreview = document.getElementById(streamPreviewId);
  
  progressBox.classList.remove("hidden");
  streamPreview.innerHTML = "[SYSTEM] Spawning runner subprocess...\n";
  
  appendToTerminal(`[SYSTEM] Starting task run to ${url}...`);

  fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  })
  .then(res => res.json())
  .then(data => {
    if (data.error) {
      streamPreview.innerHTML += `[ERROR] Failed to start task: ${data.error}\n`;
      appendToTerminal(`[ERROR] Task spawn failed: ${data.error}`, true);
      return;
    }
    
    // Poll task progress
    const taskId = data.task_id;
    pollTaskLogs(taskId, streamPreviewId, progressBoxId);
  })
  .catch(err => {
    streamPreview.innerHTML += `[CRASH] Subprocess runner exception: ${err.message}\n`;
    appendToTerminal(`[CRASH] HTTP request crashed: ${err.message}`, true);
  });
}

function pollTaskLogs(taskId, streamPreviewId, progressBoxId) {
  const streamPreview = document.getElementById(streamPreviewId);
  const progressBox = document.getElementById(progressBoxId);
  
  let lastLogsLength = 0;
  
  const timer = setInterval(async () => {
    try {
      // 1. Fetch new logs
      const logRes = await fetch(`/api/task/${taskId}/logs`);
      const logData = await logRes.json();
      
      if (logData.logs && logData.logs.length > lastLogsLength) {
        const newText = logData.logs.substring(lastLogsLength);
        lastLogsLength = logData.logs.length;
        
        // Append to local tab progress log
        streamPreview.innerHTML = logData.logs;
        streamPreview.scrollTop = streamPreview.scrollHeight;
        
        // Stream to the global Terminal Console line-by-line
        newText.split("\n").forEach(line => {
          if (line.trim()) {
            appendToTerminal(line);
          }
        });
      }
      
      // 2. Query status
      const statusRes = await fetch(`/api/task/${taskId}`);
      const statusData = await statusRes.json();
      
      if (statusData.status === "completed") {
        clearInterval(timer);
        progressBox.classList.add("hidden");
        appendToTerminal(`[SYSTEM] Task ${taskId} finished successfully.`);
        
        // Load diagnostics metrics update
        loadDiagnostics();
        
        // Render specific results
        renderTaskResults(statusData);
      } else if (statusData.status === "failed") {
        clearInterval(timer);
        progressBox.classList.add("hidden");
        
        const errMsg = statusData.error || "Unknown subprocess pipeline failure";
        appendToTerminal(`[ERROR] Task ${taskId} failed: ${errMsg}`, true);
        alert(`Checker Task Failed: ${errMsg}`);
      }
    } catch (err) {
      console.error("Polling error:", err);
    }
  }, 800);
}

// Renders specific views based on task completion results
function renderTaskResults(task) {
  const taskType = task.type;
  
  if (taskType === "cad-validator") {
    cadResultsData = task.report || {};
    cadResultsData.output_dir = task.output_dir;
    
    // Toggle active tab view to visualizer panel
    document.querySelectorAll(".nav-item").forEach(btn => {
      if (btn.getAttribute("data-tab") === "tab-cad-validator") btn.click();
    });
    
    // Render validation results badge score
    const scoreBadge = document.getElementById("cad-score-badge");
    const isPassed = cadResultsData.passed;
    const score = cadResultsData.image_comparison_overall !== undefined 
      ? cadResultsData.image_comparison_overall 
      : (cadResultsData.overall_score !== undefined ? cadResultsData.overall_score : 0.0);
      
    scoreBadge.className = `score-badge ${isPassed ? 'passed' : 'failed'}`;
    scoreBadge.textContent = `RESULT: ${isPassed ? 'PASSED ✓' : 'DIFFS DETECTED ✗'} (${(score * 100).toFixed(1)}%)`;
    
    // Reset view buttons to Front
    document.querySelectorAll("#cad-view-selector button").forEach(btn => {
      btn.classList.remove("active");
      if (btn.getAttribute("data-view") === "front") btn.classList.add("active");
    });
    
    renderCadImageResults(task.output_dir);
    
  } else if (taskType === "dimension-checker") {
    // Switch to Dimension tab
    document.querySelectorAll(".nav-item").forEach(btn => {
      if (btn.getAttribute("data-tab") === "tab-dimension-checker") btn.click();
    });
    
    const results = task.results;
    
    // Set validation overlay image
    const overlayImg = document.getElementById("dim-result-img");
    overlayImg.src = `/api/file?path=${encodeURIComponent(results.image_path)}&t=${Date.now()}`;
    overlayImg.className = ""; // Remove placeholder styling
    
    // Set summary text
    document.getElementById("dim-summary-badge").textContent = results.summary;
    
    // Fill matched dimensions table
    const tableBody = document.querySelector("#dim-results-table tbody");
    tableBody.innerHTML = "";
    
    results.matches.forEach(m => {
      const tr = document.createElement("tr");
      
      const tdId = document.createElement("td");
      tdId.textContent = m.id;
      
      const tdDim = document.createElement("td");
      tdDim.innerHTML = `<strong>${m.dim.toFixed(0)} mm</strong>`;
      
      const tdCad = document.createElement("td");
      tdCad.textContent = `${m.cad.toFixed(2)} units`;
      
      const tdType = document.createElement("td");
      tdType.textContent = m.type;
      
      const tdStatus = document.createElement("td");
      const badge = document.createElement("span");
      const isOk = m.status === "OK";
      badge.className = `status-badge ${isOk ? 'ok' : 'fail'}`;
      badge.textContent = m.status;
      tdStatus.appendChild(badge);
      
      tr.appendChild(tdId);
      tr.appendChild(tdDim);
      tr.appendChild(tdCad);
      tr.appendChild(tdType);
      tr.appendChild(tdStatus);
      tableBody.appendChild(tr);
    });
    
  } else if (taskType === "notes-checker") {
    // Switch to Notes Checker tab
    document.querySelectorAll(".nav-item").forEach(btn => {
      if (btn.getAttribute("data-tab") === "tab-notes-checker") btn.click();
    });
    
    const results = task.results;
    
    // Calculate pass rate
    const passed = results.filter(r => r.RESULT === "PASS").length;
    const failed = results.filter(r => r.RESULT === "FAIL").length;
    const total = results.length;
    
    document.getElementById("notes-summary-badge").innerHTML = `
      <strong>${passed}</strong> PASS / <strong>${failed}</strong> FAIL (${(passed/total*100).toFixed(0)}% Pass Rate)
    `;
    
    const tableBody = document.querySelector("#notes-results-table tbody");
    tableBody.innerHTML = "";
    
    results.forEach(row => {
      const tr = document.createElement("tr");
      
      const tdField = document.createElement("td");
      tdField.innerHTML = `<strong>${row.FIELD}</strong>`;
      
      const tdExp = document.createElement("td");
      tdExp.textContent = row["EXPECTED (DOCX)"];
      
      const tdFound = document.createElement("td");
      tdFound.textContent = row["FOUND IN CAD"] || "(not found)";
      
      const tdResult = document.createElement("td");
      const badge = document.createElement("span");
      const statusClass = row.RESULT.toLowerCase();
      badge.className = `status-badge ${statusClass}`;
      badge.textContent = row.RESULT;
      tdResult.appendChild(badge);
      
      tr.appendChild(tdField);
      tr.appendChild(tdExp);
      tr.appendChild(tdFound);
      tr.appendChild(tdResult);
      tableBody.appendChild(tr);
    });
    
  } else if (taskType === "nozzle-validator") {
    // Switch to Nozzle Validator tab
    document.querySelectorAll(".nav-item").forEach(btn => {
      if (btn.getAttribute("data-tab") === "tab-nozzle-validator") btn.click();
    });
    
    const results = task.results;
    const wrapper = document.getElementById("nozzle-report-wrapper");
    const openBtn = document.getElementById("btn-open-nozzle-report");
    
    // Embed output HTML report in an iframe
    const htmlReportPath = results.html_report_path;
    wrapper.innerHTML = `<iframe src="/api/file?path=${encodeURIComponent(htmlReportPath)}&t=${Date.now()}"></iframe>`;
    
    // Enable external report viewer click
    openBtn.classList.remove("hidden");
    openBtn.onclick = () => {
      window.open(`/api/file?path=${encodeURIComponent(htmlReportPath)}`, "_blank");
    };
  }
}

// Dynamic CAD visualization rendering (Front/Back buttons toggle modes)
function renderCadImageResults(taskOutputDir = null) {
  if (!cadResultsData) return;
  
  const activeView = document.querySelector("#cad-view-selector button.active").getAttribute("data-view");
  const activeMode = document.querySelector("#cad-mode-selector button.active").getAttribute("data-mode");
  
  // Resolve output dir relative to cached run
  // If taskOutputDir is not provided, query task run_dir path from history logic
  const runsFolder = taskOutputDir || cadResultsData.output_dir;
  const imageFilename = `img_${activeView}_${activeMode}.png`;
  const imageAbsPath = `${runsFolder}/image_comparison/${imageFilename}`;
  
  const targetImg = document.getElementById("cad-result-img");
  targetImg.src = `/api/file?path=${encodeURIComponent(imageAbsPath)}&t=${Date.now()}`;
  targetImg.className = ""; // Remove placeholder grey class
  
  // Render sub view metrics in text below bar
  const viewData = cadResultsData.views && cadResultsData.views[activeView];
  const metricsTxt = document.getElementById("cad-view-metrics");
  if (viewData && viewData.image_comparison) {
    const comp = viewData.image_comparison;
    metricsTxt.innerHTML = `
      Overlap: <strong>${(comp.overlap_score*100).toFixed(1)}%</strong> | 
      Model Coverage: <strong>${(comp.model_coverage*100).toFixed(1)}%</strong> | 
      Drawing Coverage: <strong>${(comp.drawing_coverage*100).toFixed(1)}%</strong>
    `;
  } else {
    metricsTxt.textContent = "Metrics unavailable for this view";
  }
}

// Double click past runs log triggers viewing the results
async function loadPastRunResults(run) {
  appendToTerminal(`[SYSTEM] Retrieving past results for Run: ${run.task_id}...`);
  try {
    const res = await fetch(`/api/task/${run.task_id}`);
    const data = await res.json();
    
    if (data.status === "completed") {
      // Re-hydrate the report/results visualizers
      renderTaskResults(data);
    } else {
      alert(`Run ${run.task_id} failed or did not output valid files: ${data.error || "incomplete execution"}`);
    }
  } catch (err) {
    console.error("Failed to load past run:", err);
    alert("Exception loading past run details.");
  }
}
