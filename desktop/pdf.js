"use strict";

/* PDF report generation.
 *
 * Built with Electron's own printToPDF rather than a PDF library: the app
 * already ships Chromium, so the report is authored as HTML/CSS and rendered by
 * the same engine. No extra dependency, and the layout is editable by anyone who
 * can read CSS.
 *
 * The layout is landscape because a lead row carries eight meaningful columns;
 * in portrait the email and LinkedIn columns end up unreadably narrow.
 */

function esc(v) {
  return String(v === null || v === undefined ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Long values (emails, URLs) need a break opportunity or they overflow the cell. */
function breakable(v) {
  return esc(v).replace(/([@._/-])/g, "$1<wbr>");
}

function shortLinkedIn(url) {
  const m = String(url || "").match(/linkedin\.com\/(company|in)\/([^/?#]+)/i);
  return m ? `${m[1]}/${m[2]}` : "";
}

function summarise(rows) {
  const n = rows.length;
  const has = (f) => rows.filter((r) => String(r[f] || "").trim()).length;
  const verified = rows.filter((r) => r.email_confidence === "verified").length;
  return {
    total: n,
    email: has("email"),
    verified,
    phone: has("phone"),
    rera: has("rera_number"),
    linkedin: has("linkedin"),
    rate: n ? Math.round((100 * has("email")) / n) : 0,
  };
}

function buildHtml(rows, meta) {
  const s = summarise(rows);
  const when = new Date().toLocaleString("en-IN", {
    dateStyle: "medium", timeStyle: "short",
  });

  const stat = (k, v, sub) =>
    `<div class="stat"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>` +
    `<div class="sub">${esc(sub || "")}</div></div>`;

  const body = rows.map((r, i) => {
    const conf = r.email_confidence || "";
    const confCls = conf === "verified" ? "ok" : conf === "generic" ? "warn" : "";
    const li = shortLinkedIn(r.linkedin);
    return `<tr>
      <td class="num dim">${i + 1}</td>
      <td>
        <div class="nm">${esc(r.name)}</div>
        <div class="sub">${esc([r.category, r.full_address].filter(Boolean).join(" · "))}</div>
      </td>
      <td>${esc(r.district || r.city || "—")}</td>
      <td class="mono tel">${esc(r.phone || "—")}</td>
      <td class="mono sm">${r.email ? breakable(r.email) : "—"}
        ${conf ? `<span class="tag ${confCls}">${esc(conf)}</span>` : ""}</td>
      <td class="mono sm">${li ? breakable(li) : "—"}</td>
      <td class="mono sm">${r.rera_number ? breakable(r.rera_number) : "—"}</td>
      <td class="num">${Number(r.active_projects) || "—"}</td>
      <td class="num strong">${Number(r.lead_score) || 0}</td>
    </tr>`;
  }).join("");

  return `<!doctype html>
<html><head><meta charset="utf-8"><title>Builder Leads</title>
<style>
  @page { size: A4 landscape; margin: 12mm 10mm 14mm; }
  * { box-sizing: border-box; }
  body {
    margin: 0; color: #16181d;
    font: 8.6pt/1.42 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
  .mono { font-family: "SF Mono", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }

  header { border-bottom: 2px solid #16181d; padding-bottom: 8px; margin-bottom: 12px; }
  h1 { margin: 0; font-size: 15pt; letter-spacing: -0.01em; }
  .meta { color: #6a7280; font-size: 8pt; margin-top: 3px; }

  .stats { display: flex; gap: 0; margin: 0 0 12px; border: 1px solid #dfe3ea; border-radius: 4px; overflow: hidden; }
  .stat { flex: 1; padding: 6px 9px; border-right: 1px solid #eef1f5; }
  .stat:last-child { border-right: 0; }
  .stat .k { font-size: 6.6pt; text-transform: uppercase; letter-spacing: .05em; color: #6a7280; }
  .stat .v { font-size: 13pt; font-weight: 650; margin-top: 1px; font-variant-numeric: tabular-nums; }
  .stat .sub { font-size: 6.8pt; color: #8b93a1; }
  .stat.hero .v { color: #0f7f4f; }

  /* Fixed layout so column widths come from the colgroup rather than from
     whichever row happens to hold the longest email. */
  table { width: 100%; border-collapse: collapse; table-layout: fixed; }
  thead { display: table-header-group; }   /* repeat the header on every page */
  th {
    text-align: left; font-size: 6.8pt; text-transform: uppercase; letter-spacing: .05em;
    color: #4a5160; border-bottom: 1px solid #c9cfd8; padding: 5px 5px; background: #f4f6f9;
  }
  td { padding: 5px; border-bottom: 1px solid #eef1f5; vertical-align: top; word-break: break-word; }
  tr { page-break-inside: avoid; }         /* never split a lead across pages */
  /* Numbers and phone numbers must never wrap: a split "10" reads as 1 and 0,
     and a phone broken across lines cannot be dialled from the page. */
  .num { text-align: right; white-space: nowrap; }
  td.tel { white-space: nowrap; }
  .strong { font-weight: 650; }
  .dim { color: #9aa2b1; }
  .sm { font-size: 7.4pt; }
  .nm { font-weight: 600; }
  .sub { color: #7b8393; font-size: 7pt; margin-top: 1px; }
  .tag {
    display: inline-block; margin-left: 3px; padding: 0 3px; border-radius: 2px;
    font-size: 6.2pt; font-family: inherit; background: #eef1f5; color: #5b6270;
  }
  .tag.ok { background: #e2f6ec; color: #0f7f4f; }
  .tag.warn { background: #fbf1d8; color: #8a6100; }

  .note { margin-top: 12px; color: #7b8393; font-size: 7pt; line-height: 1.5; }
</style></head><body>
  <header>
    <h1>Builder Leads — ${esc(meta.location || "All")}</h1>
    <div class="meta">${esc(rows.length)} builders · generated ${esc(when)}${
      meta.filters ? " · " + esc(meta.filters) : ""}</div>
  </header>

  <div class="stats">
    ${stat("Verified email", s.verified, s.total ? `${Math.round(100 * s.verified / s.total)}% of ${s.total}` : "")}
    ${stat("Any email", s.email, `${s.rate}% fill rate`)}
    ${stat("With phone", s.phone, "callable")}
    ${stat("LinkedIn", s.linkedin, "company pages")}
    ${stat("RERA verified", s.rera, "registered promoters")}
    ${stat("Total", s.total, "after broker filter")}
  </div>

  <table>
    <colgroup>
      <col style="width:3.2%"><col style="width:26%"><col style="width:7.5%">
      <col style="width:9.5%"><col style="width:19%"><col style="width:16%">
      <col style="width:11%"><col style="width:3.9%"><col style="width:4.4%">
    </colgroup>
    <thead><tr>
      <th class="num">#</th><th>Builder</th><th>District</th><th>Phone</th>
      <th>Email</th><th>LinkedIn</th><th>RERA no.</th>
      <th class="num">Proj</th><th class="num">Score</th>
    </tr></thead>
    <tbody>${body || '<tr><td colspan="9">No leads.</td></tr>'}</tbody>
  </table>

  <div class="note">
    Contacts are gathered from public sources: state RERA registries, Google Maps
    listings, and the builders' own websites. Email confidence — <b>verified</b>:
    found on the company site and the domain accepts mail; <b>generic</b>: a role
    address such as info@; otherwise found on the site but unconfirmed.
    Business contact data for B2B outreach; check DND before cold-calling mobiles.
  </div>
</body></html>`;
}

module.exports = { buildHtml, summarise };
