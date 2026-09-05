"use strict";

/* Renderer for Builder Leads.
 *
 * Reads the scraper's NDJSON event stream (status / progress / lead / stats /
 * done / error, plus an `exit` the main process synthesizes) and renders it.
 * Runs with contextIsolation on, so every privileged call goes through
 * window.api from preload.js.
 */

const $ = (id) => document.getElementById(id);

const el = {
  location: $("location"), maxResults: $("maxResults"), source: $("source"),
  start: $("startBtn"), stop: $("stopBtn"),
  stats: $("stats"),
  sVerified: $("sVerified"), sVerifiedSub: $("sVerifiedSub"),
  sEmail: $("sEmail"), sEmailSub: $("sEmailSub"), sPhone: $("sPhone"),
  sRera: $("sRera"), sSite: $("sSite"), sTotal: $("sTotal"),
  phases: {
    registry: $("phaseRegistry"), maps: $("phaseMaps"),
    enrich: $("phaseEnrich"), score: $("phaseScore"),
  },
  statusMsg: $("statusMsg"), progress: $("progress"),
  onlyEmail: $("onlyEmail"), onlyVerified: $("onlyVerified"),
  keepBrokers: $("keepBrokers"), search: $("search"),
  showing: $("showing"), exportBtn: $("exportBtn"),
  table: $("leads"), tbody: $("leadsBody"), empty: $("emptyState"),
  detail: $("detail"), detailName: $("detailName"), detailBody: $("detailBody"),
  detailClose: $("detailClose"),
  logBody: $("logBody"), logCount: $("logCount"), autoscroll: $("autoscroll"),
  clearLogs: $("clearLogs"), toggleLogs: $("toggleLogs"),
  registryBtn: $("registryBtn"), registryDot: $("registryDot"),
  registryLabel: $("registryLabel"), registryModal: $("registryModal"),
  registryClose: $("registryClose"), harvestBtn: $("harvestBtn"),
  harvestPages: $("harvestPages"),
  regProjects: $("regProjects"), regDistricts: $("regDistricts"),
  regCompanies: $("regCompanies"), regLast: $("regLast"),
  regDistrictList: $("regDistrictList"),
  settingsBtn: $("settingsBtn"), settingsModal: $("settingsModal"),
  settingsCancel: $("settingsCancel"), settingsSave: $("settingsSave"),
  apifyToken: $("apifyToken"), pythonPath: $("pythonPath"),
  tokenState: $("tokenState"), pythonState: $("pythonState"),
};

let leads = [];          // every lead from the current run, in arrival order
let selected = null;     // index into `leads`
let running = false;
let mode = "search";
let logCount = 0;

/* ------------------------------------------------------------------ helpers */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function setPhase(name, state) {
  const node = el.phases[name];
  if (!node) return;
  node.classList.remove("active", "done", "err");
  if (state) node.classList.add(state);
}

function resetPhases() {
  Object.keys(el.phases).forEach((k) => setPhase(k, null));
}

function setRunning(on) {
  running = on;
  el.start.disabled = on;
  el.stop.disabled = !on;
  el.harvestBtn.disabled = on;
  el.progress.classList.toggle("on", on);
  el.start.textContent = on ? "Working…" : "Find builders";
}

function log(text, cls) {
  const line = document.createElement("div");
  line.className = "logline" + (cls ? " ev-" + cls : "");
  const ts = new Date().toLocaleTimeString([], { hour12: false });
  line.innerHTML = `<span class="ts">${ts}</span>${esc(text)}`;
  el.logBody.appendChild(line);
  logCount += 1;
  el.logCount.textContent = `${logCount} events`;
  if (el.autoscroll.checked) el.logBody.scrollTop = el.logBody.scrollHeight;
}

/* -------------------------------------------------------------------- table */

function emailBadge(lead) {
  const email = (lead.email || "").trim();
  if (!email) return '<span class="badge none">none</span>';
  const conf = lead.email_confidence || "";
  const cls = conf === "verified" ? "verified" : conf === "generic" ? "generic" : "site";
  const label = conf === "verified" ? "verified" : conf === "generic" ? "role" : "found";
  return `<div class="cellflex">
      <span class="t mono">${esc(email)}</span>
      <span class="badge ${cls}">${label}</span>
    </div>`;
}

function scoreCell(score) {
  const n = Number(score) || 0;
  const cls = n >= 70 ? "hi" : n >= 40 ? "" : "lo";
  // Width is applied after insertion via CSSOM, not as a style attribute:
  // the page's CSP is `style-src 'self'`, which blocks inline style attributes.
  return `<div class="score">
      <span class="track"><span class="fill ${cls}" data-w="${Math.max(2, n)}"></span></span>
      <span class="n">${n}</span>
    </div>`;
}

/** Rows the toolbar filters currently allow through. */
function visibleLeads() {
  const q = el.search.value.trim().toLowerCase();
  return leads.filter((l) => {
    if (el.onlyEmail.checked && !(l.email || "").trim()) return false;
    if (el.onlyVerified.checked && l.email_confidence !== "verified") return false;
    if (!q) return true;
    return ["name", "city", "district", "email", "phone", "rera_number", "promoter_name"]
      .some((f) => String(l[f] ?? "").toLowerCase().includes(q));
  });
}

function renderTable(animateFromIndex) {
  const rows = visibleLeads();
  el.showing.textContent = `${rows.length} shown`;
  el.table.hidden = rows.length === 0;
  el.empty.hidden = rows.length > 0;

  if (rows.length === 0) {
    el.tbody.innerHTML = "";
    if (leads.length > 0) {
      el.empty.innerHTML =
        "<h3>Nothing matches those filters</h3><p>Loosen the filters above to see the rest of the run.</p>";
    }
    return;
  }

  el.tbody.innerHTML = rows.map((l, i) => {
    const idx = leads.indexOf(l);
    const rera = (l.rera_number || "").trim()
      ? `<span class="badge rera">RERA</span>` : "";
    const isNew = animateFromIndex !== undefined && idx >= animateFromIndex;
    return `<tr data-i="${idx}" class="${idx === selected ? "selected" : ""}${isNew ? " enter" : ""}">
      <td>${scoreCell(l.lead_score)}</td>
      <td>
        <div class="cellflex">
          <span class="t cname">${esc(l.name)}</span>
          ${rera}
        </div>
        ${l.category ? `<div class="csub">${esc(l.category)}</div>` : ""}
      </td>
      <td>${esc(l.district || l.city || "") || '<span class="muted">—</span>'}</td>
      <td class="mono">${esc(l.phone) || '<span class="muted">—</span>'}</td>
      <td>${emailBadge(l)}</td>
      <td class="num mono">${Number(l.active_projects) || '<span class="muted">—</span>'}</td>
      <td class="mono muted src">${esc(l.sources || "")}</td>
    </tr>`;
  }).join("");

  // Apply score-bar widths through CSSOM (allowed) rather than markup (blocked).
  el.tbody.querySelectorAll(".fill[data-w]").forEach((n) => {
    n.style.width = n.dataset.w + "%";
  });
}

el.tbody.addEventListener("click", (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  selected = Number(tr.dataset.i);
  renderTable();
  showDetail(leads[selected]);
});

const DETAIL_FIELDS = [
  ["Lead score", "lead_score"], ["RERA number", "rera_number"],
  ["Promoter", "promoter_name"], ["Registered projects", "active_projects"],
  ["Phone", "phone"], ["Email", "email"], ["Email confidence", "email_confidence"],
  ["Website", "website"], ["Category", "category"], ["Address", "full_address"],
  ["District", "district"], ["City", "city"], ["Pincode", "postal_code"],
  ["Rating", "rating"], ["Reviews", "reviews_count"], ["Sources", "sources"],
];

function showDetail(lead) {
  if (!lead) return;
  el.detail.hidden = false;
  el.detailName.textContent = lead.name || "Details";
  el.detailBody.innerHTML = DETAIL_FIELDS.map(([label, key]) => {
    let v = lead[key];
    if (v === "" || v === null || v === undefined || v === 0) {
      if (key !== "lead_score" && key !== "active_projects") return "";
    }
    if (key === "website" && v) {
      v = `<a href="${esc(v)}" target="_blank" rel="noreferrer">${esc(v)}</a>`;
    } else if (key === "email" && v) {
      v = `<span class="mono">${esc(v)}</span>`;
    } else {
      v = esc(v);
    }
    return `<dt>${label}</dt><dd>${v}</dd>`;
  }).join("");

  if (lead.google_maps_url) {
    el.detailBody.innerHTML +=
      `<dt>Google Maps</dt><dd><a href="${esc(lead.google_maps_url)}" target="_blank" rel="noreferrer">Open listing</a></dd>`;
  }
}

el.detailClose.addEventListener("click", () => { el.detail.hidden = true; });

/* -------------------------------------------------------------------- stats */

function renderStats(s) {
  const pct = (n) => (s.total ? `${Math.round((100 * n) / s.total)}% of ${s.total}` : "—");
  el.sVerified.textContent = s.verified_email ?? 0;
  el.sVerifiedSub.textContent = pct(s.verified_email ?? 0);
  el.sEmail.textContent = s.with_email ?? 0;
  el.sEmailSub.textContent = `${s.email_rate ?? 0}% fill rate`;
  el.sPhone.textContent = s.with_phone ?? 0;
  el.sRera.textContent = s.with_rera ?? 0;
  el.sSite.textContent = s.with_website ?? 0;
  el.sTotal.textContent = s.total ?? 0;
  el.stats.classList.add("show");
}

/* ------------------------------------------------------------- event stream */

window.api.onEvent((evt) => {
  switch (evt.type) {
    case "status": {
      const src = evt.source || "-";
      const msg = evt.message || evt.state || "";
      el.statusMsg.textContent = msg;
      if (src === "rera") setPhase("registry", evt.state === "done" ? "done" : "active");
      if (src === "apify" || src === "browser") setPhase("maps", "active");
      if (src === "enrich") setPhase("enrich", "active");
      if (src === "-") setPhase("score", "active");
      log(`[${src}] ${msg}`, evt.state === "fallback" ? "fallback" : null);
      break;
    }
    case "progress":
      el.statusMsg.textContent = `${evt.found} found…`;
      break;
    case "lead": {
      const before = leads.length;
      leads.push(evt.data);
      renderTable(before);
      break;
    }
    case "stats":
      renderStats(evt.data);
      break;
    case "done":
      setPhase("score", "done");
      if (mode === "harvest") {
        log(`Harvest complete — ${evt.total} projects.`, "done");
        refreshRegistry();
      } else {
        log(`Done — ${evt.total} builders → ${evt.csv_path}`, "done");
      }
      el.statusMsg.textContent = `Finished · ${evt.total} results`;
      break;
    case "error":
      log(evt.message, "error");
      el.statusMsg.textContent = evt.message;
      setPhase("score", "err");
      break;
    case "exit":
      setRunning(false);
      break;
    default:
      break;
  }
});

/* ------------------------------------------------------------------ actions */

el.start.addEventListener("click", async () => {
  const location = el.location.value.trim();
  if (!location) {
    el.location.focus();
    el.statusMsg.textContent = "Enter a location first.";
    return;
  }
  mode = "search";
  leads = [];
  selected = null;
  el.detail.hidden = true;
  el.stats.classList.remove("show");
  el.empty.innerHTML =
    "<h3>Searching…</h3><p>Reading the registry, then sweeping Google Maps for contacts.</p>";
  renderTable();
  resetPhases();
  setRunning(true);
  log(`Searching builders in ${location}…`, "system");

  const res = await window.api.startScrape({
    mode: "search",
    location,
    maxResults: Number(el.maxResults.value) || 100,
    source: el.source.value,
    onlyWithEmail: el.onlyEmail.checked,
    onlyVerifiedEmail: el.onlyVerified.checked,
    keepBrokers: el.keepBrokers.checked,
  });
  if (!res.ok) {
    log(res.error, "error");
    setRunning(false);
  }
});

el.stop.addEventListener("click", async () => {
  const res = await window.api.stopScrape();
  if (!res.ok) log(res.error, "error");
});

el.harvestBtn.addEventListener("click", async () => {
  mode = "harvest";
  resetPhases();
  setPhase("registry", "active");
  setRunning(true);
  el.registryModal.hidden = true;
  const pages = Number(el.harvestPages.value) || 200;
  log(`Harvesting the MahaRERA registry (${pages} pages)…`, "system");
  const res = await window.api.startScrape({ mode: "harvest", maxPages: pages });
  if (!res.ok) {
    log(res.error, "error");
    setRunning(false);
  }
});

el.exportBtn.addEventListener("click", async () => {
  const rows = visibleLeads();
  if (!rows.length) {
    el.statusMsg.textContent = "Nothing to export.";
    return;
  }
  const res = await window.api.exportFiltered(rows);
  if (res.ok) log(`Exported ${res.count} leads → ${res.path}`, "done");
  else if (!res.canceled) log(res.error || "Export failed.", "error");
});

[el.onlyEmail, el.onlyVerified].forEach((n) =>
  n.addEventListener("change", () => renderTable()));
el.search.addEventListener("input", () => renderTable());

el.clearLogs.addEventListener("click", () => {
  el.logBody.innerHTML = "";
  logCount = 0;
  el.logCount.textContent = "";
});
el.toggleLogs.addEventListener("click", () => {
  el.logBody.hidden = !el.logBody.hidden;
  el.toggleLogs.textContent = el.logBody.hidden ? "Show" : "Hide";
});

/* ----------------------------------------------------------------- registry */

async function refreshRegistry() {
  const s = await window.api.registryStatus();
  if (!s || !s.ok) {
    el.registryLabel.textContent = "registry unavailable";
    el.registryDot.className = "dot";
    return;
  }
  const n = s.reraProjects || 0;
  el.registryLabel.textContent = n
    ? `${n.toLocaleString()} projects`
    : "registry empty";
  el.registryDot.className = "dot " + (n ? "ok" : "empty");

  el.regProjects.textContent = n.toLocaleString();
  el.regDistricts.textContent = (s.reraDistricts || []).length;
  el.regCompanies.textContent = (s.companies || 0).toLocaleString();
  el.regLast.textContent = s.lastHarvest ? s.lastHarvest.replace("_", " ") : "never";

  // Districts double as shortcuts — clicking one runs that search.
  el.regDistrictList.innerHTML = (s.reraDistricts || [])
    .slice(0, 24)
    .map((d) => `<span class="chipx" data-d="${esc(d.name)}">${esc(d.name)}<b>${d.count}</b></span>`)
    .join("");
}

el.regDistrictList.addEventListener("click", (e) => {
  const chip = e.target.closest(".chipx");
  if (!chip) return;
  el.location.value = chip.dataset.d;
  el.registryModal.hidden = true;
  el.start.click();
});

el.registryBtn.addEventListener("click", async () => {
  await refreshRegistry();
  el.registryModal.hidden = false;
});
el.registryClose.addEventListener("click", () => { el.registryModal.hidden = true; });

/* ----------------------------------------------------------------- settings */

async function loadSettings() {
  const s = await window.api.getSettings();
  el.tokenState.textContent = s.hasToken
    ? "A token is saved. Apify is used first, with the browser as fallback."
    : "No token saved — the browser scraper will be used (slower, and it scrapes Maps directly).";
  el.pythonPath.value = s.pythonPath || "";
  el.pythonState.textContent = s.autoVenv
    ? `Using the project virtualenv: ${s.resolvedPython}`
    : `Using: ${s.resolvedPython}`;
}

el.settingsBtn.addEventListener("click", async () => {
  await loadSettings();
  el.apifyToken.value = "";
  el.settingsModal.hidden = false;
});
el.settingsCancel.addEventListener("click", () => { el.settingsModal.hidden = true; });
el.settingsSave.addEventListener("click", async () => {
  await window.api.saveSettings({
    apifyToken: el.apifyToken.value,
    pythonPath: el.pythonPath.value,
  });
  el.apifyToken.value = "";
  await loadSettings();
  el.settingsModal.hidden = true;
  log("Settings saved.", "system");
});

// Escape closes whichever modal is open; Enter in the location field searches.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    el.settingsModal.hidden = true;
    el.registryModal.hidden = true;
  }
});
el.location.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !running) el.start.click();
});

refreshRegistry();
loadSettings();
renderTable();
