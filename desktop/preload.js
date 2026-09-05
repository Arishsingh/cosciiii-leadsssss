"use strict";

const { contextBridge, ipcRenderer } = require("electron");

// Minimal, explicit IPC surface. The renderer never touches Node or the
// filesystem directly — everything privileged goes through these calls.
contextBridge.exposeInMainWorld("api", {
  startScrape: (opts) => ipcRenderer.invoke("scrape:start", opts),
  stopScrape: () => ipcRenderer.invoke("scrape:stop"),
  listRuns: () => ipcRenderer.invoke("runs:list"),
  openRun: (filePath) => ipcRenderer.invoke("run:open", filePath),
  revealOutput: () => ipcRenderer.invoke("output:reveal"),
  exportFiltered: (rows) => ipcRenderer.invoke("export:filtered", rows),
  exportPdf: (payload) => ipcRenderer.invoke("export:pdf", payload),
  registryStatus: () => ipcRenderer.invoke("registry:status"),
  getSettings: () => ipcRenderer.invoke("settings:get"),
  saveSettings: (next) => ipcRenderer.invoke("settings:save", next),

  // Event stream from the scraper process.
  onEvent: (cb) => {
    const listener = (_e, evt) => cb(evt);
    ipcRenderer.on("scrape:event", listener);
    return () => ipcRenderer.removeListener("scrape:event", listener);
  },
});
