const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"]/g, ch =>
  ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;"}[ch]));
const money = value => value == null ? "—" : "$" + Number(value).toLocaleString(
  undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const dateTime = value => value ? new Date(Number(value) * 1000).toLocaleString() : "—";
const ACTIONS = ["AUTO_APPROVE", "AUTO_REJECT", "REVIEW", "HOLD_FOR_INFO", "ESCALATE"];
const STATUSES = ["pending", "approved", "rejected", "auto_approved", "auto_rejected"];
const EMAIL_STATUSES = ["SENT", "FAILED", "PENDING", "OUTBOX", "NOT_REQUIRED"];
let vendors = [];
let currentFilters = {};
let liveCursor = 0;
const stageRows = {};

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = "Request failed";
    try { message = (await response.json()).detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function adminHeaders(headers = {}) {
  const token = $("adminToken").value.trim();
  return token ? {...headers, "X-Admin-Token": token} : headers;
}

function filterQuery() {
  const query = new URLSearchParams();
  const values = {
    date_from: $("dateFrom").value,
    date_to: $("dateTo").value,
    vendor_id: $("filterVendor").value,
    action: $("filterAction").value,
    status: $("filterStatus").value,
    email_status: $("filterEmail").value
  };
  Object.entries(values).forEach(([key, value]) => { if (value) query.set(key, value); });
  return query.toString();
}

function setOptions(id, values, placeholder) {
  const select = $(id);
  select.innerHTML = `<option value="">${esc(placeholder)}</option>` +
    values.map(value => `<option value="${esc(value)}">${esc(value.replaceAll("_", " "))}</option>`).join("");
}

function initFilters() {
  setOptions("filterAction", ACTIONS, "All decisions");
  setOptions("filterStatus", STATUSES, "All statuses");
  setOptions("filterEmail", EMAIL_STATUSES, "All email statuses");
  $("filterVendor").innerHTML = '<option value="">All vendors</option>' +
    vendors.map(v => `<option value="${esc(v.id)}">${esc(v.name)}</option>`).join("");
}

function activateTab(page) {
  document.querySelectorAll(".tab").forEach(button =>
    button.classList.toggle("active", button.dataset.page === page));
  $("overview").hidden = page !== "overview";
  document.querySelector("aside").hidden = page !== "overview";
  document.querySelectorAll(".tab-page").forEach(panel =>
    panel.hidden = panel.id !== page);
}

function bars(id, values, labelKey, valueKey, formatter = value => value) {
  const el = $(id);
  if (!values.length || values.every(item => !Number(item[valueKey]))) {
    el.innerHTML = '<div class="chart-empty">No invoice data for this filter.</div>';
    return;
  }
  const max = Math.max(1, ...values.map(item => Number(item[valueKey]) || 0));
  el.innerHTML = `<div class="chart-bars">${values.map(item => {
    const value = Number(item[valueKey]) || 0;
    const width = value ? Math.max(2, 100 * value / max) : 0;
    return `<div class="chart-row"><span class="chart-label">${esc(item[labelKey])}</span>` +
      `<span class="chart-track"><i class="chart-fill" style="width:${width}%"></i></span>` +
      `<b class="chart-value">${esc(formatter(value))}</b></div>`;
  }).join("")}</div>`;
}

function trendChart(items) {
  const el = $("chart-trends");
  if (!items.length) {
    el.innerHTML = '<div class="chart-empty">No processing activity yet.</div>';
    return;
  }
  const width = 720, height = 190, pad = 32;
  const max = Math.max(1, ...items.map(item => item.total));
  const x = i => pad + i * (width - 2 * pad) / Math.max(1, items.length - 1);
  const y = value => height - pad - value / max * (height - 2 * pad);
  const points = key => items.map((item, i) => `${x(i)},${y(item[key])}`).join(" ");
  const ticks = items.length > 1 ? [items[0], items[Math.floor((items.length - 1) / 2)], items.at(-1)] : items;
  el.innerHTML = `<p class="legend">Processed <span class="pass">● automated</span> · ` +
    `<span class="fail">● human-handled</span></p><svg class="chart-line" viewBox="0 0 ${width} ${height}" role="img" aria-label="Daily invoice processing trends">` +
    `<line x1="${pad}" x2="${width-pad}" y1="${height-pad}" y2="${height-pad}" stroke="var(--line)"/>` +
    `<polyline points="${points("automated")}" fill="none" stroke="var(--ok)" stroke-width="3"/>` +
    `<polyline points="${points("human")}" fill="none" stroke="var(--info)" stroke-width="3"/>` +
    `${ticks.map(item => `<text x="${x(items.indexOf(item))}" y="${height-7}" text-anchor="middle" fill="var(--mut)" font-size="10">${esc(item.day)}</text>`).join("")}</svg>`;
}

function automationCurve(items) {
  const el = $("curve");
  if (!items.length) {
    el.innerHTML = '<div class="chart-empty">No actual invoice activity yet.</div>';
    return;
  }
  const width = 720, height = 155, pad = 28;
  const x = i => pad + i * (width - 2 * pad) / Math.max(1, items.length - 1);
  const y = item => height - pad - (item.total ? item.automated / item.total : 0) * (height - 2 * pad);
  const points = items.map((item, i) => `${x(i)},${y(item)}`).join(" ");
  el.innerHTML = `<svg class="chart-line" viewBox="0 0 ${width} ${height}" role="img" aria-label="Automation rate over time">` +
    `<line x1="${pad}" x2="${width-pad}" y1="${height-pad}" y2="${height-pad}" stroke="var(--line)"/>` +
    `<polyline points="${points}" fill="none" stroke="var(--acc)" stroke-width="3"/>` +
    `${items.map((item,i) => `<circle cx="${x(i)}" cy="${y(item)}" r="3" fill="var(--acc)"/>`).join("")}</svg>`;
}

function vendorRates(items) {
  const el = $("chart-vendor-rates");
  if (!items.length || items.every(item => !item.total)) {
    el.innerHTML = '<div class="chart-empty">No vendor outcomes for this filter.</div>';
    return;
  }
  el.innerHTML = `<div class="chart-bars">${items.filter(item => item.total).map(item =>
    `<div class="chart-row"><span class="chart-label">${esc(item.vendor)}</span>` +
    `<span class="rate-pair"><span class="rate-line approved"><span class="chart-track"><i class="chart-fill" style="width:${item.approved_rate}%"></i></span><small>${item.approved_rate}% approved</small></span>` +
    `<span class="rate-line rejected"><span class="chart-track"><i class="chart-fill" style="width:${item.rejected_rate}%"></i></span><small>${item.rejected_rate}% rejected</small></span></span>` +
    `<b class="chart-value">${item.total}</b></div>`).join("")}</div>`;
}

function drawAnalytics(data) {
  const k = data.kpis;
  $("kpi-total").textContent = k.total_processed;
  $("kpi-auto-approved").textContent = k.auto_approved;
  $("kpi-auto-rejected").textContent = k.auto_rejected;
  $("kpi-pending").textContent = k.pending_review;
  $("kpi-hold").textContent = k.on_hold;
  $("kpi-escalated").textContent = k.escalated;
  $("kpi-value").textContent = money(k.total_value);
  $("kpi-rate").textContent = `${k.automation_rate}%`;
  $("auto").textContent = `${k.automation_rate}%`;
  $("autoSub").textContent = `automated decisions (${k.auto_approved} approvals + ${k.auto_rejected} rejections)`;
  trendChart(data.trends);
  automationCurve(data.trends);
  bars("chart-decisions", data.decision_distribution, "action", "count");
  bars("chart-vendors", data.vendor_volume, "vendor", "count");
  vendorRates(data.vendor_performance);
  bars("chart-monthly", data.monthly_value, "month", "value", money);
  bars("chart-mode", data.processing_mode, "mode", "count");
  bars("chart-emails", data.email_delivery, "status", "count");
  renderVendorPerformance(data.vendor_performance);
  renderRecent(data.recent_activity);
}

function runTableRow(row, detailed = false) {
  return `<tr class="click" data-invoice-id="${esc(row.id)}">` +
    `<td>${esc(row.invoice_number || row.filename || "—")}</td><td>${esc(row.vendor)}</td>` +
    `${detailed ? `<td>${esc(row.po_id || "—")}</td>` : ""}` +
    `<td>${money(row.total)}</td><td><span class="badge ${esc(row.action)}">${esc(row.action)}</span></td>` +
    `<td>${esc(row.status.replaceAll("_", " "))}</td>` +
    `${detailed ? "" : `<td>${esc(row.email_status)}</td>`}` +
    `${detailed ? `<td><span class="status-pill status-${esc(row.email_status)}">${esc(row.email_status)}</span></td>` : ""}` +
    `${detailed ? `<td>${esc(dateTime(row.created_at))}</td>` : ""}</tr>`;
}

function renderRecent(items) {
  $("recent").innerHTML = items.length ? items.map(row =>
    `<tr class="click" data-invoice-id="${esc(row.id)}"><td>${esc(row.invoice_number || row.filename || "—")}</td>` +
    `<td>${esc(row.vendor)}</td><td>${money(row.total)}</td>` +
    `<td><span class="badge ${esc(row.action)}">${esc(row.action)}</span></td>` +
    `<td>${esc(dateTime(row.created_at))}</td><td><span class="status-pill status-${esc(row.email_status)}">${esc(row.email_status)}</span></td></tr>`
  ).join("") : '<tr><td colspan="6" class="mut">No invoices match these filters.</td></tr>';
}

function renderVendorPerformance(items) {
  $("vendorPerformance").innerHTML = items.map(vendor =>
    `<tr><td>${esc(vendor.vendor)}</td><td><form class="vendor-email-form" data-vendor-id="${esc(vendor.vendor_id)}">` +
    `<input aria-label="Email contact for ${esc(vendor.vendor)}" type="email" value="${esc(vendor.email)}" placeholder="Add vendor email">` +
    `<button type="submit">Save</button></form></td><td>${vendor.total}</td><td>${vendor.approved}</td>` +
    `<td>${vendor.rejected}</td><td>${vendor.escalated}</td>` +
    `<td>${vendor.trust_score}/${vendor.trust_threshold}</td><td>${money(vendor.total_value)}</td>` +
    `<td>${vendor.automation_eligible ? "Eligible" : "Not eligible"}</td></tr>`
  ).join("") || '<tr><td colspan="9" class="mut">No vendor data.</td></tr>';
  document.querySelectorAll(".vendor-email-form").forEach(form => form.onsubmit = saveVendorEmail);
}

async function saveVendorEmail(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const email = form.querySelector("input").value.trim();
  try {
    await request(`/api/vendors/${encodeURIComponent(form.dataset.vendorId)}/email`, {
      method: "PUT", headers: adminHeaders({"Content-Type": "application/json"}),
      body: JSON.stringify({email})
    });
    await refresh();
  } catch (error) { alert(error.message); }
}

async function refresh() {
  try {
    const query = filterQuery();
    const suffix = query ? `?${query}` : "";
    const [analytics, invoiceRows, history] = await Promise.all([
      request(`/api/analytics${suffix}`),
      request(`/api/runs${suffix}`),
      request(`/api/email-history${suffix}`)
    ]);
    drawAnalytics(analytics);
    $("invoiceRows").innerHTML = invoiceRows.length ?
      invoiceRows.map(row => runTableRow(row, true)).join("") :
      '<tr><td colspan="8" class="mut">No invoices match these filters.</td></tr>';
    $("runs").innerHTML = invoiceRows.length ? invoiceRows.slice(0, 20).map(row =>
      `<tr class="click" data-invoice-id="${esc(row.id)}"><td>${esc(row.invoice_number || row.filename || "—")}</td>` +
      `<td>${esc(row.vendor)}</td><td>${esc(row.po_id || "—")}</td><td>${money(row.total)}</td>` +
      `<td><span class="badge ${esc(row.action)}">${esc(row.action)}</span></td><td>${esc(row.status)}</td>` +
      `<td><span class="status-pill status-${esc(row.email_status)}">${esc(row.email_status)}</span></td></tr>`
    ).join("") : '<tr><td colspan="7" class="mut">No invoices yet.</td></tr>';
    $("emailRows").innerHTML = history.length ? history.map(item =>
      `<tr><td class="click" data-invoice-id="${esc(item.run_id)}">${esc(item.invoice_number || "—")}</td>` +
      `<td>${esc(item.vendor)}</td><td>${esc(item.recipient || "—")}</td><td>${esc(item.subject)}</td>` +
      `<td><span class="status-pill status-${esc(item.status)}">${esc(item.status)}</span></td>` +
      `<td>${esc(dateTime(item.completed_at || item.created_at))}</td>` +
      `<td>${item.status === "OUTBOX" ? `<details><summary>${esc(item.error || "Drafted; not sent")}</summary><pre>${esc(item.body)}</pre></details>` : `<span class="failure">${esc(item.error || "")}</span>`}</td>` +
      `<td>${item.status === "FAILED" ? `<button data-resend="${item.run_id}:${item.id}">Resend</button>` : ""}</td></tr>`
    ).join("") : '<tr><td colspan="8" class="mut">No email notifications match these filters.</td></tr>';
    document.querySelectorAll("[data-invoice-id]").forEach(row =>
      row.onclick = () => openInvoice(row.dataset.invoiceId));
    document.querySelectorAll("[data-resend]").forEach(button =>
      button.onclick = event => { event.stopPropagation(); resendEmail(button.dataset.resend); });
    await loadProps();
  } catch (error) {
    console.error("Dashboard refresh failed:", error);
    $("recent").innerHTML = `<tr><td colspan="6" class="fail">${esc(error.message)}</td></tr>`;
  }
}

async function openInvoice(id) {
  try {
    const result = await request(`/api/runs/${encodeURIComponent(id)}`);
    activateTab("overview");
    showDecision(id, result);
  } catch (error) { alert(error.message); }
}

function showDecision(id, result) {
  const decision = result.decision;
  const panel = $("decision");
  panel.hidden = false;
  const checks = (decision.checks || []).map(check =>
    `<tr><td>${esc(check.rule)}</td><td class="${esc(check.status)}">${esc(check.status)}${check.hard ? " (hard stop)" : ""}</td><td>${esc(check.detail)}</td></tr>`
  ).join("");
  const history = result.email_history || [];
  const emailHistory = history.length ? history.map(item =>
    `<div class="detail-email"><b>${esc(item.status)}</b> · ${esc(item.recipient || "No registered recipient")} · ${esc(dateTime(item.completed_at || item.created_at))}` +
    `${item.error ? `<p class="failure">${esc(item.error)}</p>` : ""}` +
    `<p><b>${esc(item.subject)}</b></p><pre>${esc(item.body)}</pre>` +
    `${item.status === "FAILED" ? `<button onclick="resendEmail('${id}:${item.id}')">Resend email</button>` : ""}</div>`
  ).join("") : '<p class="mut">No email notification was required or recorded.</p>';
  const email = decision.vendor_email;
  panel.innerHTML = `<h2>Invoice decision</h2><p><span class="badge ${esc(decision.action)}">${esc(decision.action)}</span> ` +
    `· recommendation: <b>${esc(decision.recommendation)}</b> · confidence: <b>${esc(decision.confidence)}</b> · status: <b>${esc(result.status)}</b></p>` +
    `<p><b>Sender:</b> ${result.sender ? esc(result.sender) : "Not applicable (no email sender)"}</p>` +
    `${result.original_run_id ? `<p>Resubmitted from <button onclick="openInvoice('${esc(result.original_run_id)}')">${esc(result.original_run_id)}</button></p>` : ""}` +
    `${result.superseded_by ? `<p>Superseded by <button onclick="openInvoice('${esc(result.superseded_by)}')">${esc(result.superseded_by)}</button></p>` : ""}` +
    `<p>${esc(decision.decision_explanation || "")}</p><p><b>Why</b></p><ul>${(decision.reasons || []).map(reason => `<li>${esc(reason)}</li>`).join("")}</ul>` +
    `${decision.what_would_change_it?.length ? `<p><b>What would change it</b></p><ul>${decision.what_would_change_it.map(item => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}` +
    `${email ? `<div class="detail-email"><b>Email status:</b> <span class="status-pill status-${esc(email.status)}">${esc(email.status)}</span>` +
    `${email.recipient ? ` · ${esc(email.recipient)}` : ""}${email.duplicate_prevented ? " · duplicate notification prevented" : ""}` +
    `${email.error ? `<p class="failure">${esc(email.error)}</p>` : ""}</div>` : ""}` +
    `<h3>Email history</h3>${emailHistory}` +
    `<div class="table-wrap"><table><thead><tr><th>Rule</th><th>Result</th><th>Detail</th></tr></thead><tbody>${checks}</tbody></table></div>` +
    `${result.status === "pending" ? `<p><button class="pri" onclick="review('${id}','approve')">Approve</button> <button onclick="review('${id}','reject')">Reject</button>` +
      `${decision.action === "HOLD_FOR_INFO" ? ` <button onclick="resubmitInvoice('${id}')">Resubmit corrected invoice</button>` : ""}</p>` : ""}`;
  panel.scrollIntoView({behavior: "smooth", block: "start"});
}

function resubmitInvoice(id) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".pdf,.png,.jpg,.jpeg,.webp";
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    stream(`/api/runs/${encodeURIComponent(id)}/resubmit`, {method: "POST", body});
  };
  input.click();
}

async function resendEmail(key) {
  const [runId, emailId] = key.split(":");
  try {
    await request(`/api/runs/${encodeURIComponent(runId)}/emails/${emailId}/resend`, {method: "POST"});
    await openInvoice(runId);
    await refresh();
  } catch (error) { alert(error.message); }
}

async function review(id, verdict) {
  try {
    await request(`/api/runs/${encodeURIComponent(id)}/review`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({verdict})
    });
    await openInvoice(id);
    await refresh();
  } catch (error) { alert(error.message); }
}

function handleEvent(event) {
  if (event.type === "start") {
    Object.keys(stageRows).forEach(key => delete stageRows[key]);
    $("stages").innerHTML = "";
    $("decision").hidden = true;
  }
  if (event.type === "stage") {
    let row = stageRows[event.name];
    if (!row) {
      row = stageRows[event.name] = document.createElement("div");
      row.className = "stage";
      $("stages").appendChild(row);
    }
    row.className = `stage ${event.status}`;
    row.innerHTML = `<span class="dot"></span><b>${esc(event.name)}</b>` +
      `<span class="${event.status === "error" ? "fail" : ""}">${esc(event.summary)}</span>` +
      `<span class="mut mono">${event.ms ? `${event.ms}ms` : ""}</span>`;
  }
  if (event.type === "error") $("stages").insertAdjacentHTML("beforeend", `<p class="fail">${esc(event.message)}</p>`);
  if (event.type === "decision") {
    openInvoice(event.run_id);
    refresh();
  }
}

async function stream(url, options) {
  try {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error((await response.json()).detail || "Request failed");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const lines = buffer.split("\n");
      buffer = lines.pop();
      lines.filter(Boolean).forEach(line => handleEvent(JSON.parse(line)));
    }
    if (buffer.trim()) handleEvent(JSON.parse(buffer));
    await refresh();
  } catch (error) {
    $("stages").insertAdjacentHTML("beforeend", `<p class="fail">${esc(error.message)}</p>`);
  }
}

async function loadProps() {
  try {
    const proposals = await request("/api/proposals");
    $("props").innerHTML = proposals.length ? proposals.map(item =>
      `<p>You approved ${esc(item.vendor)} over its PO ${item.count} times (up to ${item.max_over}% over). ` +
      `Make ${item.proposed}% their tolerance?</p>` +
      `<p class="mut">Backtest: ${item.backtest.decisions_would_change} past decisions would change; ` +
      `${item.backtest.extra_auto_approval_eligible} extra invoices would be eligible for auto-approval; ` +
      `${item.backtest.later_human_rejections} of those were later rejected by a human.</p>` +
      `<p><button onclick="acceptProp('${esc(item.vendor_id)}',${item.proposed})">Accept</button></p>`
    ).join("") : "None yet. They appear after repeated overrides.";
  } catch (error) { $("props").textContent = error.message; }
}

async function acceptProp(vendorId, tolerance) {
  try {
    await request("/api/proposals/accept", {
      method: "POST", headers: adminHeaders({"Content-Type": "application/json"}),
      body: JSON.stringify({vendor_id: vendorId, tol: tolerance / 100})
    });
    await refresh();
  } catch (error) { alert(error.message); }
}

async function pollLive() {
  try {
    const data = await request(`/api/live?after=${liveCursor}`);
    liveCursor = data.cursor;
    data.events.forEach(handleEvent);
  } catch (error) { console.error("Live event polling failed:", error); }
}

async function initialize() {
  try {
    vendors = await request("/api/vendors");
    initFilters();
    const initial = await request("/api/live?after=999999");
    liveCursor = initial.cursor;
    await request("/api/samples").then(samples => {
      $("samples").innerHTML = samples.map(sample =>
        `<button data-sample="${esc(sample.key)}">${esc(sample.label)}</button>`
      ).join("");
      document.querySelectorAll("[data-sample]").forEach(button =>
        button.onclick = () => stream(`/api/sample/${encodeURIComponent(button.dataset.sample)}`, {method: "POST"}));
    });
    await refresh();
  } catch (error) {
    $("recent").innerHTML = `<tr><td colspan="6" class="fail">${esc(error.message)}</td></tr>`;
  }
}

document.querySelectorAll(".tab").forEach(button =>
  button.onclick = () => activateTab(button.dataset.page));
["dateFrom", "dateTo", "filterVendor", "filterAction", "filterStatus", "filterEmail"].forEach(id =>
  $(id).addEventListener("change", refresh));
$("clearFilters").onclick = () => {
  ["dateFrom", "dateTo", "filterVendor", "filterAction", "filterStatus", "filterEmail"].forEach(id => $(id).value = "");
  refresh();
};
$("go").onclick = () => {
  const file = $("file").files[0];
  if (!file) return alert("Choose an invoice file first");
  const body = new FormData();
  body.append("file", file);
  body.append("source", "upload");
  stream("/api/run", {method: "POST", body});
};
$("reset").onclick = async () => {
  if (!confirm("Reset demo invoices and trust data?")) return;
  try {
    await request("/api/reset", {method: "POST", headers: adminHeaders()});
    $("stages").textContent = "Demo data cleared.";
    $("decision").hidden = true;
    await refresh();
  } catch (error) { alert(error.message); }
};
$("gaunt").onclick = async () => {
  $("score").textContent = "Running...";
  try {
    const result = await request("/api/gauntlet", {method: "POST"});
    $("score").innerHTML = `<p><b>Handled correctly ${result.correct} of ${result.total}.</b> ` +
      `False escalations on clean invoices: <b>${result.false_escalations}</b>.</p>` +
      `<div class="table-wrap"><table><thead><tr><th>Case</th><th>Expected</th><th>Got</th><th>Result</th><th>Why the rules missed it</th></tr></thead><tbody>` +
      `${result.cases.map(item => `<tr><td>${esc(item.label)}</td><td>${esc(item.expected)}</td><td>${esc(item.got)}</td>` +
        `<td class="${item.ok ? "pass" : "fail"}">${item.ok ? "correct" : "missed"}</td>` +
        `<td>${item.ok ? "" : esc(item.note || "The current checks did not detect the hostile condition.")}</td></tr>`).join("")}</tbody></table></div>`;
  } catch (error) { $("score").textContent = error.message; }
};

activateTab("overview");
initialize();
setInterval(pollLive, 1500);
setInterval(refresh, 15000);
