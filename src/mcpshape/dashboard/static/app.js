// The dashboard's one script: reads the management API, renders it, edits nothing.
//
// Every few seconds, and on the Refresh button, it fetches /api/status and /api/calls. A
// Catalog, its Drift, or a Proxy's exposed set is fetched when asked for. Nothing here works
// anything out that the Daemon does not already answer.

const STATUS = "/api/status";
const CALLS = "/api/calls";
const UPSTREAMS = "/api/upstreams";
const INTERVAL_MS = 3000;
const CALLS_LIMIT = 50;

const state = { live: null, calls: [], updatedAt: null, filter: { upstream: "", proxy: "" } };

const byId = (id) => document.getElementById(id);

async function api(path) {
  const answer = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await answer.json();
  if (!answer.ok) throw new Error(body.error || `${answer.status} at ${path}`);
  return body;
}

// --- what the page shows ------------------------------------------------------------------

function nextAttempt(upstream) {
  if (upstream.state !== "unavailable") return "";
  if (!upstream.supervised) {
    return "the keeper stopped supervising; run mcpshape daemon reload to bring it back";
  }
  if (upstream.retry_in == null) return "the next call tries again";
  return `retry in ${duration(upstream.retry_in)}`;
}

function duration(seconds) {
  return seconds < 90 ? `${Math.round(seconds)}s` : `${Math.round(seconds / 60)} min`;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function button(label, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

function renderUpstreams() {
  const body = byId("upstreams").querySelector("tbody");
  body.replaceChildren();
  const upstreams = state.live ? state.live.upstreams : [];
  byId("upstreams-empty").hidden = upstreams.length > 0;
  for (const upstream of upstreams) {
    const proxies = upstream.proxies.length ? upstream.proxies : [null];
    proxies.forEach((proxy, index) => {
      const row = document.createElement("tr");
      row.appendChild(cell(index === 0 ? upstream.name : "", "name"));
      const stateCell = cell(index === 0 ? upstream.state : "", `state ${upstream.state}`);
      if (index === 0 && upstream.error) stateCell.title = upstream.error;
      if (index === 0) {
        const when = nextAttempt(upstream);
        if (when) {
          const small = document.createElement("small");
          small.textContent = ` (${when})`;
          stateCell.appendChild(small);
        }
        if (upstream.missing_command) {
          const small = document.createElement("small");
          small.textContent = ` command ${upstream.missing_command} not found`;
          stateCell.appendChild(small);
        }
      }
      row.appendChild(stateCell);
      row.appendChild(cell(proxy ? proxy.name : ""));
      const health = cell(proxy ? proxy.health : "", proxy ? `health ${proxy.health}` : "");
      if (proxy && proxy.detail) health.title = proxy.detail;
      row.appendChild(health);
      const actions = document.createElement("td");
      if (index === 0) {
        actions.appendChild(button("Catalog", () => showCatalog(upstream.name)));
        actions.appendChild(button("Drift", () => showDrift(upstream.name)));
      }
      if (proxy) actions.appendChild(button("Exposed", () => showExposed(upstream.name, proxy.name)));
      row.appendChild(actions);
      body.appendChild(row);
    });
  }
  renderFilters(upstreams);
}

function renderFilters(upstreams) {
  const upstreamSelect = byId("filter-upstream");
  const proxySelect = byId("filter-proxy");
  const names = upstreams.map((u) => u.name);
  state.filter.upstream = fillSelect(upstreamSelect, names, state.filter.upstream);
  const chosen = upstreams.find((u) => u.name === state.filter.upstream);
  const proxies = chosen ? chosen.proxies.map((p) => p.name) : [];
  state.filter.proxy = fillSelect(proxySelect, proxies, state.filter.proxy);
  proxySelect.disabled = !chosen;
}

// Rebuilds the options only when the names changed, so an open popup is not closed under
// the user by the next poll; answers the choice that is still there, or none.
function fillSelect(select, names, chosen) {
  const current = names.includes(chosen) ? chosen : "";
  const shown = [...select.options].slice(1).map((option) => option.value);
  if (shown.length === names.length && shown.every((name, i) => name === names[i])) {
    select.value = current;
    return current;
  }
  select.replaceChildren();
  const all = document.createElement("option");
  all.value = "";
  all.textContent = "all";
  select.appendChild(all);
  for (const name of names) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.appendChild(option);
  }
  select.value = current;
  return current;
}

function renderCalls() {
  const body = byId("calls").querySelector("tbody");
  body.replaceChildren();
  const calls = [...state.calls].reverse();
  byId("calls-empty").hidden = calls.length > 0;
  for (const call of calls) {
    const row = document.createElement("tr");
    row.appendChild(cell(new Date(call.at).toLocaleTimeString()));
    row.appendChild(cell(`${call.upstream}/${call.proxy}`));
    const tool = cell(call.exposed);
    if (call.exposed !== call.name) tool.title = `Catalog name: ${call.name}`;
    row.appendChild(tool);
    row.appendChild(cell(call.outcome, `outcome ${call.outcome}`));
    row.appendChild(cell(`${Math.round(call.duration_ms)} ms`));
    const answer = cell(call.result, "answer");
    answer.title = `${call.result_chars} characters`;
    row.appendChild(answer);
    body.appendChild(row);
  }
}

function renderUpdated() {
  const updated = byId("updated");
  if (!state.updatedAt || document.body.classList.contains("unreachable")) return;
  const ago = Math.round((Date.now() - state.updatedAt) / 1000);
  updated.textContent = `updated ${ago}s ago`;
}

// --- what is fetched on request --------------------------------------------------------------

function showDetail(title, node) {
  byId("detail-title").textContent = title;
  byId("detail").replaceChildren(node);
  byId("detail-section").hidden = false;
}

function itemList(kind, items) {
  const list = document.createElement("ul");
  for (const [name, definition] of Object.entries(items)) {
    const li = document.createElement("li");
    li.textContent = name;
    if (definition && definition.description) {
      const p = document.createElement("small");
      p.textContent = ` ${definition.description}`;
      li.appendChild(p);
    }
    list.appendChild(li);
  }
  const wrapper = document.createElement("div");
  const h = document.createElement("h3");
  h.textContent = `${kind} (${Object.keys(items).length})`;
  wrapper.appendChild(h);
  wrapper.appendChild(list);
  return wrapper;
}

async function showCatalog(upstream) {
  try {
    const { catalog } = await api(`${UPSTREAMS}/${upstream}/catalog`);
    const node = document.createElement("div");
    if (!catalog) {
      node.textContent = `No Catalog yet: run mcpshape upstream sync ${upstream}.`;
    } else {
      const scanned = document.createElement("p");
      scanned.textContent = `scanned ${new Date(catalog.scanned_at).toLocaleString()}`;
      node.appendChild(scanned);
      for (const kind of ["tools", "resources", "resource_templates", "prompts"]) {
        node.appendChild(itemList(kind.replace("_", " "), catalog[kind]));
      }
    }
    showDetail(`Catalog of ${upstream}`, node);
  } catch (error) {
    showDetail(`Catalog of ${upstream}`, failed(error));
  }
}

async function showDrift(upstream) {
  try {
    const { drift } = await api(`${UPSTREAMS}/${upstream}/drift`);
    const node = document.createElement("div");
    if (!drift) {
      node.textContent = "No unreviewed Drift.";
    } else {
      const summary = document.createElement("p");
      summary.textContent = drift.summary;
      node.appendChild(summary);
      for (const [sign, items] of [["+", drift.added], ["-", drift.removed], ["~", drift.changed]]) {
        for (const item of items) {
          const p = document.createElement("p");
          p.textContent = `${sign} ${item.kind.replace("_", " ")} ${item.name}`;
          node.appendChild(p);
        }
      }
      if (drift.instructions_changed) {
        const p = document.createElement("p");
        p.textContent = "~ instructions";
        node.appendChild(p);
      }
      const how = document.createElement("p");
      how.textContent = `Review it with: mcpshape upstream sync ${upstream}`;
      node.appendChild(how);
    }
    showDetail(`Drift of ${upstream}`, node);
  } catch (error) {
    showDetail(`Drift of ${upstream}`, failed(error));
  }
}

async function showExposed(upstream, proxy) {
  const title = `What ${upstream}/${proxy} exposes`;
  try {
    const exposed = await api(`${UPSTREAMS}/${upstream}/proxies/${proxy}/exposed`);
    const node = document.createElement("div");
    if (exposed.health !== "ok") {
      const warning = document.createElement("p");
      warning.className = "error";
      warning.textContent = `This Proxy is ${exposed.health}: ${exposed.detail || ""}. ` +
        "What follows is the last exposed set it derived, which it keeps advertising.";
      node.appendChild(warning);
    }
    if (!exposed.scanned) {
      const notYet = document.createElement("p");
      notYet.textContent = `No Catalog yet: run mcpshape upstream sync ${upstream}.`;
      node.appendChild(notYet);
    }
    const name = document.createElement("p");
    name.textContent = `server name: ${exposed.name}`;
    node.appendChild(name);
    if (exposed.instructions) {
      const p = document.createElement("p");
      p.textContent = `instructions: ${exposed.instructions}`;
      node.appendChild(p);
    }
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const label of ["Kind", "Exposed as", "Catalog name", "Description"]) {
      const th = document.createElement("th");
      th.textContent = label;
      headRow.appendChild(th);
    }
    head.appendChild(headRow);
    table.appendChild(head);
    const body = document.createElement("tbody");
    for (const item of exposed.items) {
      const row = document.createElement("tr");
      if (item.hidden) row.className = "hidden-item";
      row.appendChild(cell(item.kind.replace("_", " ")));
      row.appendChild(cell(item.hidden ? "(hidden)" : item.name));
      row.appendChild(cell(item.virtual ? "(Virtual Tool)" : item.origin));
      row.appendChild(cell(item.description || ""));
      body.appendChild(row);
    }
    table.appendChild(body);
    node.appendChild(table);
    showDetail(title, node);
  } catch (error) {
    showDetail(title, failed(error));
  }
}

function failed(error) {
  const p = document.createElement("p");
  p.className = "error";
  p.textContent = String(error.message || error);
  return p;
}

// --- the polling -------------------------------------------------------------------------------

function callsPath() {
  const params = new URLSearchParams({ limit: String(CALLS_LIMIT) });
  if (state.filter.upstream) params.set("upstream", state.filter.upstream);
  if (state.filter.proxy) params.set("proxy", state.filter.proxy);
  return `${CALLS}?${params}`;
}

async function refresh() {
  try {
    const [live, calls] = await Promise.all([api(STATUS), api(callsPath())]);
    state.live = live;
    state.calls = calls.calls;
    state.updatedAt = Date.now();
    document.body.classList.remove("unreachable");
  } catch (error) {
    document.body.classList.add("unreachable");
    byId("updated").textContent = `not updated: ${error.message || error}`;
    return;
  }
  renderUpstreams();
  renderCalls();
  renderUpdated();
}

byId("refresh").addEventListener("click", refresh);
byId("filter-upstream").addEventListener("change", (event) => {
  state.filter.upstream = event.target.value;
  state.filter.proxy = "";
  refresh();
});
byId("filter-proxy").addEventListener("change", (event) => {
  state.filter.proxy = event.target.value;
  refresh();
});
byId("daemon-url").textContent = window.location.origin;

refresh();
setInterval(refresh, INTERVAL_MS);
setInterval(renderUpdated, 1000);
