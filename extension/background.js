/* Orchestration lives here rather than in the popup: a capture can take a
 * minute, and the popup dies the moment you click away from it.
 *
 * File downloads also happen here, not in the page. A page-context fetch is
 * bound by CORS, and while api.printables.com sends the headers to allow it,
 * files.printables.com does not — which failed as a bare "NetworkError" with
 * no indication that CORS was the cause. A background fetch covered by
 * host_permissions is not subject to CORS and still sends the user's cookies
 * with credentials:"include", so it gets the best of both. */

const DEFAULTS = {
  endpoint: "",
  token: "",
  fetchFiles: true,
  defaultStatus: "wishlist",
};

const MAX_TOTAL_BYTES = 400 * 1024 * 1024;

async function cfg() {
  return Object.assign({}, DEFAULTS, await browser.storage.local.get(Object.keys(DEFAULTS)));
}

async function api(path, { method = "GET", json, body, headers } = {}) {
  const c = await cfg();
  if (!c.endpoint) throw new Error("No shelf endpoint set — open the extension options.");
  if (!c.token) throw new Error("No shelf token set — open the extension options.");
  const h = Object.assign({ Authorization: `Bearer ${c.token}` }, headers || {});
  if (json !== undefined) { h["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  const r = await fetch(c.endpoint.replace(/\/$/, "") + path, { method, headers: h, body });
  const text = await r.text();
  let data = null;
  try { data = JSON.parse(text); } catch { /* keep the raw text for the error */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status}: ${text.slice(0, 300)}`);
  return data;
}

async function extract(tabId) {
  const [res] = await browser.scripting.executeScript({
    target: { tabId }, files: ["extract.js"],
  });
  if (!res || !res.result) throw new Error("Could not read this page.");
  return res.result;
}

// ------------------------------------------------------------------ downloads

async function graphql(endpoint, query, variables) {
  const r = await fetch(endpoint, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  // parse the body even on a non-2xx: GraphQL puts the real reason in
  // errors[], and throwing on status alone discards it
  const j = await r.json().catch(() => null);
  if (j && j.errors && j.errors.length) {
    throw new Error(j.errors.map((e) => e.message).join("; ").slice(0, 300));
  }
  if (!r.ok) throw new Error(`graphql HTTP ${r.status}`);
  return j.data;
}

async function grab(url, name, out, state) {
  const r = await fetch(url, { credentials: "include" });
  if (!r.ok) { out.warnings.push(`${name}: HTTP ${r.status}`); return; }
  const ct = (r.headers.get("content-type") || "").toLowerCase();
  // A login wall answers 200 with HTML. Storing that as a .zip is worse than
  // storing nothing, because it looks like a successful capture.
  if (/text\/html/.test(ct)) {
    out.warnings.push(`${name}: got an HTML page, not a file — are you signed in?`);
    return;
  }
  const blob = await r.blob();
  if (!blob.size) { out.warnings.push(`${name}: empty response`); return; }
  state.total += blob.size;
  if (state.total > MAX_TOTAL_BYTES) {
    out.warnings.push(`${name}: skipped, over the ${MAX_TOTAL_BYTES / 1048576} MB budget`);
    return;
  }
  out.files.push({ blob, name, source_url: url });
}

const PRINTABLES_API = "https://api.printables.com/graphql/";

async function printablesLinks(modelId, known, out) {
  let files = known;
  if (!files || !files.length) {
    const d = await graphql(PRINTABLES_API,
      `{ print(id: "${modelId}") { stls { id name fileSize } } }`, {});
    files = (((d || {}).print || {}).stls) || [];
  }
  if (!files.length) throw new Error("this model lists no files");

  const links = [];
  for (const f of files.slice(0, 40)) {
    try {
      // verified against the live API: root field is `print` not `model`, the
      // mutation arg is `printId`, and `source` is the enum `model_detail`.
      // Everything under `stls` (.3mf and .step included) resolves as `stl`.
      const dl = await graphql(PRINTABLES_API,
        `mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!, $source: DownloadSourceEnum!) {
           getDownloadLink(id: $id, printId: $modelId, fileType: $fileType, source: $source) {
             ok errors { field messages } output { link ttl } } }`,
        { id: String(f.id), modelId: String(modelId), fileType: "stl", source: "model_detail" });
      const node = (dl || {}).getDownloadLink || {};
      const link = (node.output || {}).link;
      if (link) links.push({ url: link, name: f.name || `${f.id}.stl` });
      else {
        const why = (node.errors || []).flatMap((e) => e.messages || []).join(", ");
        out.warnings.push(`${f.name || f.id}: ${why || "no link returned"}`);
      }
    } catch (e) {
      out.warnings.push(`${f.name || f.id}: ${String(e.message || e).slice(0, 150)}`);
    }
  }
  if (!links.length) throw new Error("no download links returned");
  return links;
}

async function downloadFiles(plan) {
  const out = { files: [], warnings: [], method: plan.kind };
  const state = { total: 0 };
  try {
    if (plan.kind === "printables-graphql") {
      for (const l of await printablesLinks(plan.modelId, plan.files, out)) {
        await grab(l.url, l.name, out, state);
      }

    } else if (plan.kind === "thingiverse-zip") {
      await grab(plan.url, `thing-${plan.modelId || "files"}.zip`, out, state);

    } else if (plan.kind === "makerworld-api") {
      const r = await fetch(
        `https://makerworld.com/api/v1/design-service/design/${plan.modelId}`,
        { credentials: "include" });
      if (!r.ok) throw new Error(`MakerWorld API HTTP ${r.status}`);
      const j = await r.json();
      const inst = (j.instances || j.designs || [])[0] || j;
      const url = inst.packageUrl || inst.modelFileUrl || (j.design || {}).packageUrl;
      if (url) await grab(url, `makerworld-${plan.modelId}.3mf`, out, state);
      else out.warnings.push("MakerWorld API gave no package URL (sign in, or it is a paid model)");

    } else {
      out.warnings.push(plan.reason || "no automatic download for this site");
    }
  } catch (e) {
    out.warnings.push(`download failed: ${String(e.message || e).slice(0, 200)}`);
  }
  return out;
}

/* Images are public and small; fetched here alongside everything else. */
async function fetchImages(images) {
  const out = [];
  for (const img of (images || []).slice(0, 6)) {
    try {
      const r = await fetch(img.url, { credentials: "omit" });
      if (!r.ok) continue;
      const blob = await r.blob();
      if (!blob.size || blob.size > 12 * 1024 * 1024) continue;
      if (!/^image\//.test(blob.type)) continue;
      const ext = (blob.type.split("/")[1] || "jpg").replace("jpeg", "jpg").slice(0, 4);
      out.push({ blob, name: `${out.length === 0 && img.cover ? "cover" : "img" + out.length}.${ext}`,
                 cover: !!img.cover, source_url: img.url });
    } catch { /* a missing gallery image is not worth failing the capture over */ }
  }
  return out;
}

async function save({ record, tags, projects, status, note, withFiles }) {
  const c = await cfg();
  const rec = record;
  let fileResult = { files: [], warnings: [] };

  if (withFiles && c.fetchFiles && rec.downloadPlan) {
    fileResult = await downloadFiles(Object.assign({ modelId: rec.id }, rec.downloadPlan));
  }
  const images = await fetchImages(rec.images);

  const uploads = [];
  const fd = new FormData();
  fileResult.files.forEach((f, i) => {
    fd.append(`file${i}`, f.blob, f.name);
    uploads.push({ field: `file${i}`, name: f.name, source_url: f.source_url });
  });
  images.forEach((im, i) => {
    fd.append(`image${i}`, im.blob, im.name);
    uploads.push({ field: `image${i}`, name: im.name, cover: im.cover, source_url: im.source_url });
  });

  const payload = {
    source: rec.source, id: rec.id, title: rec.title, source_url: rec.source_url,
    author: rec.author, license: rec.license, published: rec.published,
    summary: rec.summary, stats: rec.stats,
    tags, projects, status: status || c.defaultStatus,
    page_html: rec.page_html, page_text: rec.page_text,
    uploads,
    capture: Object.assign({}, rec.capture, {
      method: fileResult.method || "metadata-only",
      extension_version: browser.runtime.getManifest().version,
      warnings: [...(rec.capture.warnings || []), ...(fileResult.warnings || [])],
    }),
  };
  fd.append("payload", JSON.stringify(payload));

  const res = await api("/api/save", { method: "POST", body: fd });
  if (note) {
    await api("/api/annotate", { method: "POST", json: { source: rec.source, id: rec.id, note } });
  }
  return Object.assign({}, res, { warnings: payload.capture.warnings });
}

browser.runtime.onMessage.addListener(async (msg) => {
  try {
    switch (msg.type) {
      case "extract":   return { ok: true, record: await extract(msg.tabId) };
      case "lookup":    return { ok: true, result: await api(
                          `/api/lookup?source=${encodeURIComponent(msg.source)}&id=${encodeURIComponent(msg.id)}`) };
      case "taxonomy":  return { ok: true, taxonomy: await api("/api/taxonomy") };
      case "save":      return { ok: true, result: await save(msg) };
      case "annotate":  return { ok: true, result: await api("/api/annotate", { method: "POST", json: msg.body }) };
      case "convertible": return { ok: true, result: await api(
                          `/api/convertible?source=${encodeURIComponent(msg.source)}&id=${encodeURIComponent(msg.id)}`) };
      // can take minutes; running here means closing the popup does not cancel it
      case "convert":   return { ok: true, result: await api("/api/convert", {
                          method: "POST", json: { source: msg.source, id: msg.id, files: msg.files } }) };
      case "health":    return { ok: true, health: await api("/api/health") };
      default:          return { ok: false, error: `unknown message ${msg.type}` };
    }
  } catch (e) {
    return { ok: false, error: String(e.message || e) };
  }
});
