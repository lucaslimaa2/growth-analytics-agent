// Growth Analytics Agent — frontend
// On load: fetch /api/dashboard and render the fixed dashboard.
// On submit: post to /api/chat and render text + charts + tables + summary.

// ============================================================================
// Dashboard
// ============================================================================

async function loadDashboard() {
  const mrrEl = document.getElementById("dashboard-mrr-chart");
  const cohortEl = document.getElementById("dashboard-cohort-chart");
  mrrEl.innerHTML = '<div class="thinking"><span class="dot"></span><span class="dot"></span><span class="dot"></span> Loading…</div>';
  try {
    const resp = await fetch("/api/dashboard");
    if (!resp.ok) throw new Error(`status ${resp.status}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    renderKPIs(data.kpis || []);
    renderDashboardChart(mrrEl, "line", data.mrr_chart);
    renderDashboardChart(cohortEl, "heatmap", data.cohort_chart);
    renderEvents(data.events || []);
  } catch (err) {
    mrrEl.innerHTML = `<div class="error">Failed to load dashboard: ${err.message}</div>`;
    cohortEl.innerHTML = "";
  }
}

function renderKPIs(kpis) {
  const wrap = document.getElementById("kpis");
  wrap.innerHTML = "";
  kpis.forEach((k) => {
    const card = document.createElement("div");
    card.className = "kpi";
    card.innerHTML = `
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-value">${k.value_fmt}</div>
      ${renderDelta(k)}
    `;
    wrap.appendChild(card);
  });
}

function renderDelta(k) {
  if (k.delta_pct == null) return '<div class="kpi-delta neutral">vs prior month</div>';
  const sign = k.delta_pct > 0 ? "+" : "";
  const direction = k.delta_pct > 0 ? "up" : k.delta_pct < 0 ? "down" : "neutral";
  // Color logic: "good_direction" tells us which way is desirable.
  // Up + good=up → green. Down + good=down → green. Otherwise red.
  let cls = "neutral";
  if (direction === "up") cls = k.good_direction === "up" ? "up" : "down";
  if (direction === "down") cls = k.good_direction === "down" ? "up" : "down";
  return `<div class="kpi-delta ${cls}">${sign}${k.delta_pct}% vs prior month</div>`;
}

function renderDashboardChart(target, chartType, payload) {
  if (!payload || !payload.x || !payload.x.length) {
    target.innerHTML = '<div class="error">No data</div>';
    return;
  }
  let traces;
  if (chartType === "heatmap") {
    traces = [{
      type: "heatmap",
      x: payload.x,
      y: payload.y.map((s) => s.name),
      z: payload.y.map((s) => s.values),
      colorscale: "Blues",
      hoverongaps: false,
    }];
  } else {
    traces = [{
      type: "scatter",
      mode: "lines+markers",
      x: payload.x,
      y: payload.y,
      line: { color: "#1a1a2e", width: 2 },
      marker: { size: 5 },
    }];
  }
  const layout = {
    title: { text: payload.title, font: { size: 14, family: "Inter, system-ui, sans-serif" } },
    margin: { l: 60, r: 20, t: 40, b: 50 },
    plot_bgcolor: "white",
    paper_bgcolor: "white",
    font: { family: "Inter, system-ui, sans-serif", size: 11 },
    xaxis: payload.x_label ? { title: { text: payload.x_label } } : {},
    yaxis: payload.y_label ? { title: { text: payload.y_label } } : {},
  };
  Plotly.newPlot(target, traces, layout, { responsive: true, displaylogo: false });
}

function renderEvents(events) {
  const wrap = document.getElementById("events-timeline");
  if (!events.length) { wrap.innerHTML = ""; return; }
  let html = '<h3>Annotated business events</h3>';
  events.forEach((e) => {
    html += `
      <div class="event-item">
        <div class="event-date">${e.date}</div>
        <div class="event-desc"><strong>${e.event_type}</strong> — ${e.description}</div>
      </div>
    `;
  });
  wrap.innerHTML = html;
}

// ============================================================================
// Chat
// ============================================================================

const historyEl = document.getElementById("history");
const form = document.getElementById("composer");
const input = document.getElementById("question");
const sendBtn = document.getElementById("send");

// Wire up the suggestion chips
document.querySelectorAll(".suggest").forEach((btn) => {
  btn.addEventListener("click", () => {
    input.value = btn.textContent;
    input.focus();
  });
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = input.value.trim();
  if (!question) return;

  appendUserMessage(question);
  input.value = "";
  setSending(true);

  const thinkingEl = appendThinking();

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });

    thinkingEl.remove();

    if (!resp.ok) {
      const errText = await safeText(resp);
      appendError(`Error ${resp.status}: ${errText || resp.statusText}`);
      return;
    }

    const data = await resp.json();
    if (data.error) {
      appendError(data.error);
      return;
    }
    appendAgentMessage(data);
  } catch (err) {
    thinkingEl.remove();
    appendError(`Network error: ${err.message}`);
  } finally {
    setSending(false);
    input.focus();
  }
});

function setSending(isSending) {
  sendBtn.disabled = isSending;
  sendBtn.textContent = isSending ? "Thinking…" : "Ask";
}

function appendUserMessage(text) {
  const msg = document.createElement("div");
  msg.className = "message user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  msg.appendChild(bubble);
  historyEl.appendChild(msg);
  scrollToBottom();
}

function appendThinking() {
  const msg = document.createElement("div");
  msg.className = "message agent";
  const dots = document.createElement("div");
  dots.className = "thinking";
  dots.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span> The agent is querying and analyzing…';
  msg.appendChild(dots);
  historyEl.appendChild(msg);
  scrollToBottom();
  return msg;
}

function appendError(msg) {
  const wrap = document.createElement("div");
  wrap.className = "message agent";
  const err = document.createElement("div");
  err.className = "error";
  err.textContent = msg;
  wrap.appendChild(err);
  historyEl.appendChild(wrap);
  scrollToBottom();
}

function appendAgentMessage({ answer, outputs }) {
  const msg = document.createElement("div");
  msg.className = "message agent";

  if (answer) {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = formatMarkdownish(answer);
    msg.appendChild(bubble);
  }

  if (outputs && outputs.length) {
    const wrap = document.createElement("div");
    wrap.className = "outputs";
    for (const out of outputs) {
      const el = renderOutput(out);
      if (el) wrap.appendChild(el);
    }
    msg.appendChild(wrap);
  }

  historyEl.appendChild(msg);
  scrollToBottom();
}

function renderOutput(output) {
  if (!output || !output.kind) return null;
  if (output.kind === "chart") return renderChart(output.data);
  if (output.kind === "table") return renderTable(output.data);
  if (output.kind === "summary") return renderSummary(output.data);
  return null;
}

function renderChart({ chart_id, title, spec, error }) {
  if (error) return errorBox(`chart error: ${error}`);
  const card = document.createElement("div");
  card.className = "chart-card";
  const plot = document.createElement("div");
  plot.className = "plot";
  plot.id = `plot-${chart_id || Math.random().toString(36).slice(2)}`;
  card.appendChild(plot);
  // Plotly needs to render after the element is in the DOM.
  setTimeout(() => {
    if (window.Plotly && spec) {
      Plotly.newPlot(plot, spec.data || [], spec.layout || {}, { responsive: true, displaylogo: false });
    }
  }, 0);
  return card;
}

function renderTable({ title, columns, rows, error }) {
  if (error) return errorBox(`table error: ${error}`);
  const card = document.createElement("div");
  card.className = "table-card";
  if (title) {
    const h = document.createElement("h3");
    h.textContent = title;
    card.appendChild(h);
  }
  const tbl = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  (columns || []).forEach((c) => {
    const th = document.createElement("th");
    th.textContent = c;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  tbl.appendChild(thead);

  const tbody = document.createElement("tbody");
  (rows || []).forEach((row) => {
    const tr = document.createElement("tr");
    row.forEach((val) => {
      const td = document.createElement("td");
      td.textContent = val == null ? "" : String(val);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);
  card.appendChild(tbl);
  return card;
}

function renderSummary({ insights, recommendations, error }) {
  if (error) return errorBox(`summary error: ${error}`);
  const card = document.createElement("div");
  card.className = "summary-card";

  if (insights && insights.length) {
    const h = document.createElement("h3");
    h.textContent = "Insights";
    card.appendChild(h);
    const ul = document.createElement("ul");
    insights.forEach((i) => {
      const li = document.createElement("li");
      li.innerHTML = formatMarkdownish(i);
      ul.appendChild(li);
    });
    card.appendChild(ul);
  }

  if (recommendations && recommendations.length) {
    const h = document.createElement("h3");
    h.textContent = "Recommendations";
    card.appendChild(h);
    const ul = document.createElement("ul");
    recommendations.forEach((r) => {
      const li = document.createElement("li");
      li.innerHTML = formatMarkdownish(r);
      ul.appendChild(li);
    });
    card.appendChild(ul);
  }
  return card;
}

function errorBox(msg) {
  const div = document.createElement("div");
  div.className = "error";
  div.textContent = msg;
  return div;
}

// Very small markdown subset: **bold**, `code`, line breaks.
// Escapes HTML first to avoid XSS from agent output.
function formatMarkdownish(text) {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return escaped
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}

function scrollToBottom() {
  window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
}

async function safeText(resp) {
  try { return await resp.text(); } catch { return ""; }
}

// Fire the dashboard fetch as soon as the script runs.
loadDashboard();
