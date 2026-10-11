const $ = (id) => document.getElementById(id);
const send = (msg) => browser.runtime.sendMessage(msg);

let record = null;
let tabId = null;
const chosen = { tags: new Set(), projects: new Set() };

function renderChips(kind, vocab) {
  const box = $(kind);
  box.textContent = "";
  const all = new Set([...Object.keys(vocab || {}), ...chosen[kind]]);
  for (const name of [...all].sort()) {
    const el = document.createElement("span");
    el.className = "chip" + (chosen[kind].has(name) ? " on" : "");
    el.textContent = name;
    el.title = (vocab || {})[name] || "";
    el.onclick = () => {
      chosen[kind].has(name) ? chosen[kind].delete(name) : chosen[kind].add(name);
      renderChips(kind, vocab);
    };
    box.appendChild(el);
  }
}

function wireAdd(inputId, kind, vocab) {
  $(inputId).addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const v = e.target.value.trim().toLowerCase().replace(/\s+/g, "-");
    if (v) { chosen[kind].add(v); renderChips(kind, vocab); }
    e.target.value = "";
  });
}

async function init() {
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  tabId = tab.id;

  const { endpoint } = await browser.storage.local.get("endpoint");
  $("shelf").href = endpoint ? endpoint.replace(/\/$/, "") + "/" : "#";

  const ex = await send({ type: "extract", tabId });
  if (!ex.ok) return fail(ex.error);
  record = ex.record;

  $("loading").hidden = true;
  $("body").hidden = false;
  $("src").textContent = record.source;
  $("title").textContent = record.title || "(untitled)";
  const bits = [record.author && record.author.name, record.license && record.license.name,
                record.id && `#${record.id}`].filter(Boolean);
  $("sub").textContent = bits.join(" · ");

  const warnings = (record.capture && record.capture.warnings) || [];
  if (!record.downloadPlan || record.downloadPlan.kind === "none") {
    warnings.push("no automatic file download for this site — save the page, then attach a zip");
  }
  if (warnings.length) { $("warn").hidden = false; $("warn").textContent = "⚠ " + warnings.join(" · "); }

  const tx = await send({ type: "taxonomy" });
  const vocab = tx.ok ? tx.taxonomy : { tags: {}, projects: {}, statuses: ["wishlist"] };

  const sel = $("status");
  for (const s of vocab.statuses || ["wishlist"]) {
    const o = document.createElement("option");
    o.value = o.textContent = s;
    sel.appendChild(o);
  }

  // already on the shelf? show it and pre-load what is already filed there,
  // so re-saving updates the entry instead of blanking its tags
  if (record.id) {
    const lk = await send({ type: "lookup", source: record.source, id: record.id });
    if (lk.ok && lk.result.found) {
      const r = lk.result.record || {};
      (r.tags || []).forEach((t) => chosen.tags.add(t));
      (r.projects || []).forEach((p) => chosen.projects.add(p));
      if (r.status) sel.value = r.status;
      $("existing").hidden = false;
      $("existing").textContent =
        `Already on the shelf — saved ${(r.captured || "").slice(0, 10)}, ` +
        `${(r.files || []).length} file(s). `;
      $("existing").appendChild(link(lk.result.url, "open ↗"));
      $("save").textContent = "Update on shelf";
      if ((r.files || []).length) $("withFiles").checked = false;
      const cv = await send({ type: "convertible", source: record.source, id: record.id });
      if (cv.ok) showConvert(cv.result.available, cv.result.files);
    }
  }

  renderChips("tags", vocab.tags);
  renderChips("projects", vocab.projects);
  wireAdd("tagAdd", "tags", vocab.tags);
  wireAdd("projAdd", "projects", vocab.projects);

  $("save").onclick = doSave;
}

async function doSave() {
  const btn = $("save");
  btn.disabled = true;
  btn.textContent = $("withFiles").checked ? "Downloading and committing…" : "Committing…";
  const res = await send({
    type: "save", tabId, record,
    tags: [...chosen.tags].sort(), projects: [...chosen.projects].sort(),
    status: $("status").value, note: $("note").value.trim(),
    withFiles: $("withFiles").checked,
  });
  const box = $("result");
  box.hidden = false;
  if (!res.ok) {
    box.className = "result err";
    box.textContent = "Failed: " + res.error;
    btn.disabled = false; btn.textContent = "Retry";
    return;
  }
  const r = res.result;
  box.className = "result";
  box.textContent = `Saved — ${r.files} file(s). `;
  box.appendChild(link(r.url, "open in Forgejo ↗"));
  // built as nodes, not markup: warnings carry text scraped from the page
  for (const w of r.warnings || []) {
    const div = document.createElement("div");
    div.style.marginTop = "5px";
    div.textContent = "⚠ " + w;
    box.appendChild(div);
  }
  btn.textContent = "Saved";
  showConvert(r.convert_available, r.convertible);
}

/* After a save, or for a model already on the shelf: offer its STL/3MF files
 * (zip contents included) for conversion to STEP on the server. */
function showConvert(available, files) {
  if (!available || !files || !files.length) return;
  const list = $("convList");
  list.textContent = "";
  for (const f of files) {
    const row = document.createElement("label");
    row.className = "row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = f.source;
    cb.checked = !f.step;  // default to what has no STEP yet
    cb.onchange = syncAll;
    const name = document.createElement("span");
    name.textContent = f.name;
    name.title = f.source;
    row.append(cb, name);
    if (f.step) {
      const done = document.createElement("span");
      done.className = "muted";
      done.textContent = "has STEP";
      row.appendChild(done);
    }
    list.appendChild(row);
  }
  $("convAll").onchange = (e) => {
    for (const cb of boxes()) cb.checked = e.target.checked;
    syncAll();
  };
  $("convBtn").onclick = doConvert;
  syncAll();
  $("convert").hidden = false;
}

const boxes = () => [...$("convList").querySelectorAll("input[type=checkbox]")];

function syncAll() {
  const all = boxes();
  const on = all.filter((cb) => cb.checked).length;
  $("convAll").checked = on === all.length;
  $("convAll").indeterminate = on > 0 && on < all.length;
  $("convBtn").disabled = on === 0;
  $("convBtn").textContent = on === 0 ? "Convert to STEP"
    : on === all.length ? "Convert all to STEP" : `Convert ${on} to STEP`;
}

async function doConvert() {
  const all = boxes();
  const picked = all.filter((cb) => cb.checked).map((cb) => cb.value);
  const btn = $("convBtn");
  btn.disabled = true;
  btn.textContent = `Converting ${picked.length} file(s)…`;
  const res = await send({
    type: "convert", source: record.source, id: record.id,
    files: picked.length === all.length ? "all" : picked,
  });
  const box = $("convResult");
  box.hidden = false;
  box.textContent = "";
  if (!res.ok) {
    box.className = "result err";
    box.textContent = "Conversion failed: " + res.error;
    syncAll();
    return;
  }
  const results = res.result.results || [];
  const good = results.filter((x) => x.ok);
  box.className = good.length === results.length ? "result" : "result err";
  box.textContent = `Converted ${good.length} of ${results.length} to STEP. `;
  if (res.result.url && good.length) box.appendChild(link(res.result.url, "open ↗"));
  for (const x of results.filter((y) => !y.ok)) {
    const div = document.createElement("div");
    div.style.marginTop = "5px";
    div.textContent = `✗ ${x.source.split("::").pop()}: ${(x.error || "").split("\n").pop()}`;
    box.appendChild(div);
  }
  const done = new Set(good.map((x) => x.source));
  for (const cb of all) if (done.has(cb.value)) cb.checked = false;
  syncAll();
}

function link(href, text) {
  const a = document.createElement("a");
  a.href = href;
  a.target = "_blank";
  a.textContent = text;
  return a;
}

function fail(err) {
  $("loading").textContent = err;
  $("loading").className = "warn";
}

init().catch((e) => fail(String(e.message || e)));
