/* Runs in the page. Returns a metadata record for whatever model page is open.
 * Each adapter refines the generic result; nothing is allowed to throw, because
 * a half-extracted record that tells you which site it came from is far more
 * useful than a failed capture. */

async function msExtract() {
  const txt = (el) => (el && el.innerText ? el.innerText.trim() : "");
  const q = (s, root) => (root || document).querySelector(s);
  const qa = (s, root) => [...(root || document).querySelectorAll(s)];
  const meta = (name) => {
    const el =
      q(`meta[property="${name}"]`) || q(`meta[name="${name}"]`);
    return el ? (el.getAttribute("content") || "").trim() : "";
  };
  const abs = (u) => { try { return new URL(u, location.href).href; } catch { return null; } };

  // ---------------------------------------------------------------- generic

  const jsonld = [];
  for (const s of qa('script[type="application/ld+json"]')) {
    try {
      const v = JSON.parse(s.textContent);
      jsonld.push(...(Array.isArray(v) ? v : [v]));
    } catch { /* malformed ld+json is common; ignore it */ }
  }
  const ld = jsonld.find((o) => o && /Product|CreativeWork|3DModel|Thing/.test(o["@type"] || "")) || {};

  const rec = {
    source: "web",
    id: "",
    source_url: location.href.split("#")[0],
    title: ld.name || meta("og:title") || txt(q("h1")) || document.title,
    author: {},
    license: {},
    published: ld.datePublished || null,
    summary: ld.description || meta("og:description") || "",
    stats: {},
    images: [],
    files: [],
    capture: { adapter: "generic", warnings: [] },
  };
  const ogImg = meta("og:image");
  if (ogImg) rec.images.push({ url: abs(ogImg), cover: true });

  const licLink = q('a[href*="creativecommons.org"], a[href*="opensource.org/licenses"], a[href*="gnu.org/licenses"]');
  if (licLink) rec.license = { name: txt(licLink) || licLink.href, url: licLink.href };

  const host = location.hostname.replace(/^www\./, "");
  const path = location.pathname;

  // ------------------------------------------------------------- printables

  if (host.endsWith("printables.com")) {
    rec.source = "printables";
    rec.capture.adapter = "printables";
    rec.id = (path.match(/\/model\/(\d+)/) || [])[1] || "";
    // Their public GraphQL answers unauthenticated and gives a clean author
    // name, real licence and real dates — all of which the DOM mangles.
    try {
      const p = await printablesApi(rec.id);
      rec.title = p.name || txt(q("h1")) || rec.title;
      rec.summary = p.summary || rec.summary;
      rec.published = p.firstPublish || p.datePublished || null;
      if (p.user) {
        rec.author = {
          name: p.user.publicUsername || p.user.handle,
          url: p.user.handle ? `https://www.printables.com/@${p.user.handle}` : null,
        };
      }
      if (p.license && p.license.name) {
        rec.license = { name: p.license.name, url: (rec.license || {}).url || null };
      }
      rec.images = (p.images || []).slice(0, 8).map((im, n) => ({
        url: `https://media.printables.com/${im.filePath}`, cover: n === 0,
      }));
      rec.stats = { files: (p.stls || []).length };
      rec.capture.adapter = "printables-api";
      rec.downloadPlan = {
        kind: "printables-graphql",
        modelId: rec.id,
        files: (p.stls || []).map((f) => ({ id: f.id, name: f.name, bytes: f.fileSize })),
      };
    } catch (e) {
      rec.capture.warnings.push(`printables API: ${String(e.message || e).slice(0, 160)}`);
      rec.title = txt(q("h1")) || rec.title;
      const a = q("a.author[href^='/@']") || q("a[href^='/@']");
      if (a) {
        // the user-card anchor's innerText also carries the follower count,
        // so take the first line that is neither a number nor the @handle
        const line = txt(a).split("\n").map((x) => x.trim())
          .find((x) => x && !/^@/.test(x) && !/^[\d.,kKmM]+$/.test(x));
        const handle = (a.getAttribute("href") || "").replace(/^\/@/, "").split("/")[0];
        rec.author = { name: line || handle, url: abs(`/@${handle}`) };
      }
      // NOTE: no thumbnail rewriting. An earlier version rewrote /thumbs/<size>/
      // to /media/, which 404'd every image and silently produced zero images.
      rec.images = dedupeImages(
        qa('img[src*="media.printables.com"], img[src*="files.printables.com"]')
          .map((i) => ({ url: i.currentSrc || i.src }))
          .filter((x) => !/\/avatars\/|\/(40x40|80x80)\//.test(x.url || ""))
      );
      rec.downloadPlan = { kind: "printables-graphql", modelId: rec.id };
    }
  }

  // ------------------------------------------------------------ thingiverse

  else if (host.endsWith("thingiverse.com")) {
    rec.source = "thingiverse";
    rec.capture.adapter = "thingiverse";
    rec.id = (path.match(/thing:(\d+)/) || [])[1] || "";
    // og: tags on Thingiverse are site-wide boilerplate, never per-thing —
    // read the DOM instead or you will archive the same blurb every time.
    rec.title = txt(q("h1")) || document.title.replace(/ - Thingiverse$/, "");
    rec.summary = "";
    const a = qa('a[href$="/designs"]').find((x) => txt(x));
    if (a) rec.author = { name: txt(a), url: abs(a.getAttribute("href").replace(/\/designs$/, "")) };
    const d = (document.body.innerText.match(/[A-Z][a-z]+ \d{1,2}, \d{4}/) || [])[0];
    if (d) { const p = Date.parse(d); if (!isNaN(p)) rec.published = new Date(p).toISOString().slice(0, 10); }
    rec.images = dedupeImages(
      qa("img").map((i) => ({ url: i.currentSrc || i.src }))
        .filter((x) => /resize\.thingiverse\.com|cdn\.thingiverse\.com\/assets/.test(x.url || ""))
    );
    const sum = qa("div,section").find((el) => /^Summary\b/.test(txt(el)) && txt(el).length < 8000);
    if (sum) rec.summary = txt(sum).replace(/^Summary\s*/, "").slice(0, 4000);
    rec.downloadPlan = { kind: "thingiverse-zip", url: `${location.origin}/thing:${rec.id}/zip` };
  }

  // -------------------------------------------------------------- makerworld

  else if (host.endsWith("makerworld.com")) {
    rec.source = "makerworld";
    rec.capture.adapter = "makerworld";
    rec.id = (path.match(/\/models\/(\d+)/) || [])[1] || "";
    rec.title = txt(q("h1")) || rec.title;
    const a = q('a[href*="/@"]') || q('a[href*="/u/"]');
    if (a) rec.author = { name: txt(a), url: abs(a.getAttribute("href")) };
    rec.images = dedupeImages(
      qa("img").map((i) => ({ url: i.currentSrc || i.src }))
        .filter((x) => /makerworld\.com|bambulab/.test(x.url || "") && !/avatar|icon/i.test(x.url))
    );
    rec.downloadPlan = { kind: "makerworld-api", modelId: rec.id };
  }

  // ------------------------------------------------------------------ thangs

  else if (host.endsWith("thangs.com")) {
    rec.source = "thangs";
    rec.capture.adapter = "thangs";
    rec.id = (path.match(/(\d{4,})(?:\/?$|\?)/) || [])[1] || "";
    rec.title = txt(q("h1")) || rec.title;
    rec.images = dedupeImages(qa("img").map((i) => ({ url: i.currentSrc || i.src }))
      .filter((x) => /thangs|cloudfront/.test(x.url || "")));
    rec.downloadPlan = { kind: "none", reason: "Thangs has no stable download endpoint" };
  }

  // ------------------------------------------------------------------ cults

  else if (host.endsWith("cults3d.com")) {
    rec.source = "cults3d";
    rec.capture.adapter = "cults3d";
    rec.id = (path.match(/\/3d-model\/[^/]+\/([^/?#]+)/) || [])[1] || "";
    rec.title = txt(q("h1")) || rec.title;
    rec.downloadPlan = { kind: "none", reason: "Cults downloads are per-file and paywalled" };
  }

  if (!rec.id) {
    rec.id = "";
    rec.capture.warnings.push("no stable model id found in the URL");
  }
  if (!rec.title) rec.capture.warnings.push("no title found");
  if (!rec.license.name) rec.capture.warnings.push("no licence found — check the page before reusing");

  rec.images = rec.images.slice(0, 8);
  if (rec.images.length && !rec.images.some((i) => i.cover)) rec.images[0].cover = true;

  rec.page_html = document.documentElement.outerHTML.slice(0, 3_000_000);
  rec.page_text = `# ${rec.title}\n\n<${rec.source_url}>\n\n` +
    document.body.innerText.replace(/\n{3,}/g, "\n\n").slice(0, 200_000);

  return rec;

  async function printablesApi(id) {
    const r = await fetch("https://api.printables.com/graphql/", {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        query: `{ print(id: "${id}") { id name summary datePublished firstPublish
                   filesCount user { publicUsername handle }
                   license { name } images { id filePath }
                   stls { id name fileSize } } }`,
      }),
    });
    // parse the body even on a non-2xx: GraphQL puts the real reason in
    // errors[], and throwing on status alone discards it
    const j = await r.json().catch(() => null);
    if (j && j.errors && j.errors.length) throw new Error(j.errors[0].message);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const print = ((j || {}).data || {}).print;
    if (!print) throw new Error("no print in response");
    return print;
  }

  function dedupeImages(list) {
    const seen = new Set(), out = [];
    for (const i of list) {
      const u = i.url && abs(i.url);
      if (!u || seen.has(u)) continue;
      seen.add(u);
      out.push({ url: u, cover: !!i.cover });
    }
    return out;
  }
}

msExtract();
