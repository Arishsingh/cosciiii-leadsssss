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
  location: $("location"), area: $("area"),
  districtList: $("districtList"), areaList: $("areaList"),
  maxResults: $("maxResults"), source: $("source"),
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
  showing: $("showing"), exportBtn: $("exportBtn"), exportPdfBtn: $("exportPdfBtn"),
  table: $("leads"), tbody: $("leadsBody"), empty: $("emptyState"),
  detail: $("detail"), detailName: $("detailName"), detailBody: $("detailBody"),
  detailClose: $("detailClose"),
  logBody: $("logBody"), logCount: $("logCount"), autoscroll: $("autoscroll"),
  clearLogs: $("clearLogs"), toggleLogs: $("toggleLogs"),
  brandStates: $("brandStates"), registryBtn: $("registryBtn"), registryDot: $("registryDot"),
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
const byId = new Map();  // company id -> index in `leads`, for in-place updates
let selected = null;     // index into `leads`
let pending = false;     // a render is already queued for the next frame
let running = false;
let mode = "search";
let logCount = 0;
let districtAreas = {};  // district -> [taluka, …], for the Area suggestions
let runLimit = 0;        // how many builders this run was asked for

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

/** LinkedIn as a compact link — the URL itself is long and never worth a column. */
function linkedinCell(lead) {
  const url = (lead.linkedin || "").trim();
  if (!url) return '<span class="muted">—</span>';
  const company = url.includes("/company/");
  return `<a class="li" href="${esc(url)}" target="_blank" rel="noreferrer"
     title="${esc(url)}">in<span>${company ? "company" : "profile"}</span></a>`;
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

/** Rows the toolbar filters allow through, capped to the requested count.
 *
 * The pipeline deliberately gathers more than asked — it over-fetches from the
 * registry so that after merging, broker filtering and ranking there are still
 * enough good rows to fill the request. That surplus is working material, not
 * the answer: asking for 10 builders must show 10.
 */
function visibleLeads() {
  const q = el.search.value.trim().toLowerCase();
  const matched = leads.filter((l) => {
    if (el.onlyEmail.checked && !(l.email || "").trim()) return false;
    if (el.onlyVerified.checked && l.email_confidence !== "verified") return false;
    if (!q) return true;
    return ["name", "city", "district", "email", "phone", "rera_number", "promoter_name", "linkedin"]
      .some((f) => String(l[f] ?? "").toLowerCase().includes(q));
  });
  return runLimit > 0 ? matched.slice(0, runLimit) : matched;
}

/** Everything the filters allow, before the count cap — for the "of N" label. */
function matchedCount() {
  const q = el.search.value.trim().toLowerCase();
  return leads.filter((l) => {
    if (el.onlyEmail.checked && !(l.email || "").trim()) return false;
    if (el.onlyVerified.checked && l.email_confidence !== "verified") return false;
    if (!q) return true;
    return ["name", "city", "district", "email", "phone", "rera_number", "promoter_name", "linkedin"]
      .some((f) => String(l[f] ?? "").toLowerCase().includes(q));
  }).length;
}

/* Results stream in bursts, and re-rendering the table per row would spend the
   whole run in layout. Coalescing into one render per animation frame keeps the
   UI responsive no matter how fast rows arrive. */
function scheduleRender() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    renderTable();
  });
}

function renderTable(animateFromIndex) {
  const rows = visibleLeads();
  const matched = matchedCount();
  el.showing.textContent = matched > rows.length
    ? `${rows.length} shown · ${matched.toLocaleString()} matched`
    : `${rows.length} shown`;
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
      <td>${linkedinCell(l)}</td>
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
  ["Website", "website"], ["LinkedIn", "linkedin"], ["Category", "category"], ["Address", "full_address"],
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
    if ((key === "website" || key === "linkedin") && v) {
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
      // Rows arrive more than once: a promoter published from the registry is
      // republished when the Maps sweep or enrichment fills in its contacts.
      // Keying on the company id updates that row in place instead of
      // appending a duplicate.
      const d = evt.data;
      const key = d.id;
      if (key !== undefined && byId.has(key)) {
        leads[byId.get(key)] = d;
      } else {
        if (key !== undefined) byId.set(key, leads.length);
        leads.push(d);
      }
      scheduleRender();
      break;
    }
    case "stats":
      renderStats(evt.data);
      break;
    case "done":
      setPhase("score", "done");
      // Rank only now. Sorting while rows stream in would make them jump under
      // the cursor; during the run, arrival order is already useful because the
      // registry publishes the biggest developers first.
      leads.sort((a, b) => (b.lead_score || 0) - (a.lead_score || 0) ||
                           String(a.name || "").localeCompare(String(b.name || "")));
      byId.clear();
      leads.forEach((l, i) => { if (l.id !== undefined) byId.set(l.id, i); });
      selected = null;
      renderTable();
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
  runLimit = Math.max(1, Number(el.maxResults.value) || 100);
  leads = [];
  byId.clear();
  selected = null;
  el.detail.hidden = true;
  el.stats.classList.remove("show");
  el.empty.innerHTML =
    "<h3>Searching…</h3><p>Reading the registry, then sweeping Google Maps for contacts.</p>";
  renderTable();
  resetPhases();
  setRunning(true);
  const areaVal = el.area.value.trim();
  log(`Searching builders in ${areaVal ? areaVal + ", " : ""}${location}…`, "system");

  const res = await window.api.startScrape({
    mode: "search",
    location,
    area: el.area.value.trim(),
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

el.exportPdfBtn.addEventListener("click", async () => {
  const rows = visibleLeads();
  if (!rows.length) {
    el.statusMsg.textContent = "Nothing to export.";
    return;
  }
  // Export exactly what is on screen, and record which filters produced it so
  // the report is self-explanatory once it leaves the app.
  const filters = [
    el.onlyEmail.checked && "has email",
    el.onlyVerified.checked && "verified email only",
    el.keepBrokers.checked && "brokers included",
    el.search.value.trim() && `matching "${el.search.value.trim()}"`,
  ].filter(Boolean).join(", ");

  el.exportPdfBtn.disabled = true;
  el.exportPdfBtn.textContent = "Building…";
  try {
    const res = await window.api.exportPdf({
      rows,
      meta: {
        location: [el.area.value.trim(), el.location.value.trim()]
          .filter(Boolean).join(", "),
        filters,
      },
    });
    if (res.ok) log(`Exported ${res.count} leads → ${res.path}`, "done");
    else if (!res.canceled) log(res.error || "PDF export failed.", "error");
  } finally {
    el.exportPdfBtn.disabled = false;
    el.exportPdfBtn.textContent = "Export PDF";
  }
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
  // Name the states actually loaded rather than hard-coding one.
  const states = (s.reraStates || []).map((x) =>
    x.name.charAt(0) + x.name.slice(1).toLowerCase());
  el.brandStates.textContent = states.length ? states.join(" · ") : "";

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
  // Feed the District suggestions and remember each district's areas so the
  // Area list can narrow once a district is chosen.
  districtAreas = {};
  (s.reraDistricts || []).forEach((d) => { districtAreas[d.name.toUpperCase()] = d.areas || []; });
  el.districtList.innerHTML = (s.reraDistricts || [])
    .map((d) => `<option value="${esc(d.name)}">${d.count} projects</option>`)
    .join("");
  refreshAreaList();

  el.regDistrictList.innerHTML = (s.reraDistricts || [])
    .slice(0, 24)
    .map((d) => `<span class="chipx" data-d="${esc(d.name)}">${esc(d.name)}<b>${d.count}</b></span>`)
    .join("");
}

/** Area suggestions follow the district; with none chosen, offer every area. */
function refreshAreaList() {
  const d = el.location.value.trim().toUpperCase();
  let areas = districtAreas[d];
  if (!areas) {
    // Allow a partial district name ("mumbai" -> "Mumbai Suburban").
    const hit = Object.keys(districtAreas).find((k) => d && k.includes(d));
    areas = hit ? districtAreas[hit] : [];
  }
  if (!areas.length) {
    areas = [...new Set(Object.values(districtAreas).flat())].slice(0, 60);
  }
  el.areaList.innerHTML = areas.map((a) => `<option value="${esc(a)}"></option>`).join("");
}

el.location.addEventListener("input", refreshAreaList);
el.location.addEventListener("change", refreshAreaList);

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
[el.location, el.area].forEach((n) => n.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !running) el.start.click();
}));

refreshRegistry();
loadSettings();
renderTable();
