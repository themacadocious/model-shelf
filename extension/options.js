const $ = (id) => document.getElementById(id);

browser.storage.local.get(["endpoint", "token", "fetchFiles"]).then((c) => {
  $("endpoint").value = c.endpoint || "";
  $("token").value = c.token || "";
  $("fetchFiles").checked = c.fetchFiles !== false;
});

$("save").onclick = async () => {
  if (await persist()) $("status").textContent = "saved";
};

async function persist() {
  const endpoint = $("endpoint").value.trim().replace(/\/$/, "");
  // the endpoint is user-supplied, so its permission has to be asked for at
  // runtime — a static host_permission cannot cover an unknown host
  if (endpoint) {
    try {
      const origin = new URL(endpoint).origin + "/*";
      if (!(await browser.permissions.contains({ origins: [origin] }))) {
        await browser.permissions.request({ origins: [origin] });
      }
    } catch (e) {
      $("status").textContent = ` bad endpoint URL: ${e.message}`;
      return false;
    }
  }
  await browser.storage.local.set({
    endpoint,
    token: $("token").value.trim(),
    fetchFiles: $("fetchFiles").checked,
  });
  return true;
}

$("test").onclick = async () => {
  // save first: testing whatever is in the fields is what you meant, and
  // making you press Save before Test is just a trap
  $("status").textContent = "testing…";
  if (!(await persist())) return;
  const res = await browser.runtime.sendMessage({ type: "health" });
  $("status").textContent = res.ok
    ? `ok, saved — ${res.health.repo}`
    : `failed: ${res.error}`;
};

const SITES = browser.runtime.getManifest().host_permissions;

async function showPerm() {
  const ok = await browser.permissions.contains({ origins: SITES });
  $("perm").textContent = ok ? " granted" : " not granted yet";
}
showPerm();

$("grant").onclick = async () => {
  // must be called straight from the click, or Firefox rejects it as
  // not a user gesture
  const ok = await browser.permissions.request({ origins: SITES });
  $("perm").textContent = ok ? " granted" : " denied";
};
