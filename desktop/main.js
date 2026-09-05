"use strict";

const { app, BrowserWindow, ipcMain, shell, dialog } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

// desktop/ lives inside the repo; the scraper, output/, and config.json are one up.
const REPO_ROOT = path.join(__dirname, "..");
const SCRAPER = path.join(REPO_ROOT, "scraper", "scraper.py");
const STATUS_SCRIPT = path.join(REPO_ROOT, "scraper", "status.py");
const OUTPUT_DIR = path.join(REPO_ROOT, "output");
const CONFIG_PATH = path.join(REPO_ROOT, "config.json");

let win = null;
let child = null; // the running Python scraper process, if any
let killTimer = null; // pending SIGKILL escalation after a stop request

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 820,
    title: "Builder Leads",
    minWidth: 1040,
    minHeight: 640,
    backgroundColor: "#0b0d10",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
}

// ---- config.json helpers ----------------------------------------------------

function readConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
  } catch {
    return {};
  }
}

function writeConfig(next) {
  const merged = Object.assign(readConfig(), next);
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(merged, null, 2), "utf-8");
  return merged;
}

// ---- scraper process --------------------------------------------------------

// Look for a project virtualenv's python (where playwright is typically
// installed). Checked in order; first existing one wins.
function findVenvPython() {
  const rel = process.platform === "win32"
    ? [["venv", "Scripts", "python.exe"], [".venv", "Scripts", "python.exe"], ["scraper", ".venv", "Scripts", "python.exe"]]
    : [["venv", "bin", "python"], [".venv", "bin", "python"], ["scraper", ".venv", "bin", "python"]];
  for (const parts of rel) {
    const p = path.join(REPO_ROOT, ...parts);
    try { if (fs.existsSync(p)) return p; } catch { /* ignore */ }
  }
  return null;
}

// Resolve which python to launch. An explicit interpreter set in Settings wins;
// the bare defaults ("python3"/"python") are treated as "unset" so a project
// venv is auto-preferred over the system python when one exists.
function pythonExe() {
  const configured = (readConfig().pythonPath || "").trim();
  if (configured && configured !== "python3" && configured !== "python") {
    return configured;
  }
  return findVenvPython() || configured || "python3";
}

function startScrape(_evt, opts) {
  if (child) return { ok: false, error: "A scrape is already running." };

  const mode = opts.mode === "harvest" ? "harvest" : "search";
  const args = [SCRAPER, "--mode", mode, "--output-dir", OUTPUT_DIR, "--json"];

  if (mode === "harvest") {
    // A full registry walk is ~1250 pages; the UI offers a bounded run too.
    if (opts.maxPages) args.push("--max-pages", String(opts.maxPages));
    if (opts.delay) args.push("--delay", String(opts.delay));
  } else {
    args.push("--location", String(opts.location || ""));
    args.push("--max-results", String(opts.maxResults || 100));
    const source = ["auto", "apify", "browser"].includes(opts.source) ? opts.source : "auto";
    args.push("--source", source);
    if (opts.onlyWithEmail) args.push("--only-with-email");
    if (opts.onlyVerifiedEmail) args.push("--only-verified-email");
    if (opts.keepBrokers) args.push("--keep-brokers");
    if (opts.noEnrich) args.push("--no-enrich");
    if (opts.noRera) args.push("--no-rera");
  }

  try {
    // detached:true makes the child its own process-group leader so a forced
    // kill can target the whole tree (Python + any chromium it launched).
    child = spawn(pythonExe(), args, { cwd: path.join(REPO_ROOT, "scraper"), detached: true });
  } catch (e) {
    child = null;
    return { ok: false, error: String(e) };
  }

  let buffer = "";
  const send = (evt) => {
    if (win && !win.isDestroyed()) win.webContents.send("scrape:event", evt);
  };

  child.stdout.on("data", (chunk) => {
    buffer += chunk.toString();
    let nl;
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      try {
        send(JSON.parse(line));
      } catch {
        send({ type: "status", source: "-", state: "scraping", message: line });
      }
    }
  });

  child.stderr.on("data", (chunk) => {
    send({ type: "status", source: "python", state: "scraping", message: chunk.toString().trim() });
  });

  child.on("close", (code) => {
    if (killTimer) { clearTimeout(killTimer); killTimer = null; }
    if (buffer.trim()) {
      try { send(JSON.parse(buffer.trim())); } catch { /* ignore trailing partial */ }
    }
    send({ type: "exit", code });
    child = null;
  });

  child.on("error", (e) => {
    send({ type: "error", message: `Failed to launch Python (${pythonExe()}): ${e.message}` });
    child = null;
  });

  return { ok: true };
}

function stopScrape() {
  if (!child) return { ok: false, error: "Nothing running." };

  // Immediate feedback so the UI reflects the click even if shutdown takes a beat.
  if (win && !win.isDestroyed()) {
    win.webContents.send("scrape:event", { type: "status", source: "-", state: "scraping", message: "stopping…" });
  }

  // SIGTERM to Python only: its cooperative stop flag flushes the CSV, closes the
  // browser, and exits cleanly.
  const target = child;
  target.kill("SIGTERM");

  // Safety net: if it hasn't exited shortly, force-kill the whole process group.
  killTimer = setTimeout(() => {
    if (child !== target) return; // already exited and cleared
    try {
      process.kill(-target.pid, "SIGKILL"); // negative pid = the detached group
    } catch {
      try { target.kill("SIGKILL"); } catch { /* already gone */ }
    }
  }, 4000);

  return { ok: true };
}

// ---- past runs --------------------------------------------------------------

function listRuns() {
  try {
    const files = fs.readdirSync(OUTPUT_DIR).filter((f) => f.toLowerCase().endsWith(".csv"));
    return files
      .map((f) => {
        const full = path.join(OUTPUT_DIR, f);
        const st = fs.statSync(full);
        return { name: f, path: full, size: st.size, mtime: st.mtimeMs };
      })
      .sort((a, b) => b.mtime - a.mtime);
  } catch {
    return [];
  }
}

function openRun(_evt, filePath) {
  return shell.openPath(filePath);
}

function revealOutput() {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  shell.openPath(OUTPUT_DIR);
  return { ok: true };
}

// ---- export filtered view ---------------------------------------------------

// Must stay identical to scraper/mapping.py::COLUMNS and events.py::CsvWriter,
// or the filtered export drifts from the run CSV.
const COLUMNS = [
  "name", "category", "full_address", "city", "state", "postal_code",
  "phone", "website", "email", "rating", "reviews_count", "opening_hours",
  "latitude", "longitude", "google_maps_url",
  "rera_number", "promoter_name", "promoter_type", "active_projects",
  "email_confidence", "lead_score", "sources", "linkedin",
];

function csvCell(v) {
  const s = v === null || v === undefined ? "" : String(v);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

async function exportFiltered(_evt, rows) {
  const res = await dialog.showSaveDialog(win, {
    title: "Export filtered leads",
    defaultPath: path.join(OUTPUT_DIR, "filtered-leads.csv"),
    filters: [{ name: "CSV", extensions: ["csv"] }],
  });
  if (res.canceled || !res.filePath) return { ok: false, canceled: true };

  const lines = [COLUMNS.join(",")];
  for (const r of rows || []) {
    lines.push(COLUMNS.map((c) => csvCell(r[c])).join(","));
  }
  fs.writeFileSync(res.filePath, lines.join("\n"), "utf-8");
  return { ok: true, path: res.filePath, count: (rows || []).length };
}

// ---- registry status --------------------------------------------------------

// Shelled out to Python because the main process has no SQLite driver, and
// adding a native module just to render a status chip is not worth it.
function registryStatus() {
  return new Promise((resolve) => {
    let out = "";
    let proc;
    try {
      proc = spawn(pythonExe(), [STATUS_SCRIPT], { cwd: path.join(REPO_ROOT, "scraper") });
    } catch (e) {
      resolve({ ok: false, error: String(e) });
      return;
    }
    proc.stdout.on("data", (c) => { out += c.toString(); });
    proc.on("error", (e) => resolve({ ok: false, error: e.message }));
    proc.on("close", () => {
      try {
        resolve(JSON.parse(out.trim().split("\n").pop()));
      } catch {
        resolve({ ok: false, error: "could not read registry status" });
      }
    });
  });
}

// ---- settings ---------------------------------------------------------------

function getSettings() {
  const cfg = readConfig();
  return {
    // Never send the raw token to the renderer; just whether one is set.
    hasToken: Boolean((cfg.apifyToken || "").trim()),
    pythonPath: cfg.pythonPath || "",
    // The interpreter that will actually launch, after venv auto-detection.
    resolvedPython: pythonExe(),
    autoVenv: Boolean(findVenvPython()),
  };
}

function saveSettings(_evt, next) {
  const cfg = readConfig();
  if (typeof next.apifyToken === "string" && next.apifyToken.trim()) {
    cfg.apifyToken = next.apifyToken.trim();
  }
  if (typeof next.pythonPath === "string") {
    const v = next.pythonPath.trim();
    if (v) cfg.pythonPath = v;
    else delete cfg.pythonPath; // blank = auto-detect (venv, else system python3)
  }
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2), "utf-8");
  return getSettings();
}

// ---- wiring -----------------------------------------------------------------

app.whenReady().then(() => {
  ipcMain.handle("scrape:start", startScrape);
  ipcMain.handle("scrape:stop", stopScrape);
  ipcMain.handle("runs:list", listRuns);
  ipcMain.handle("run:open", openRun);
  ipcMain.handle("output:reveal", revealOutput);
  ipcMain.handle("export:filtered", exportFiltered);
  ipcMain.handle("registry:status", registryStatus);
  ipcMain.handle("settings:get", getSettings);
  ipcMain.handle("settings:save", saveSettings);

  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  if (child) child.kill("SIGTERM");
  if (process.platform !== "darwin") app.quit();
});
