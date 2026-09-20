#!/usr/bin/env python3
"""Sign an extension through Mozilla's AMO API, unlisted (self-distribution).

Usage:
    tools/amo-sign.py dist/model-shelf-0.4.0.xpi

Credentials come from ~/.amo-credentials (or the environment):

    AMO_JWT_ISSUER=user:12345678:123
    AMO_JWT_SECRET=<64 hex characters>

Get them from https://addons.mozilla.org/developers/addon/api/key/ — see the
README. The first run for a given add-on id creates the add-on; later runs add
a version to it.

Three things that cost time if you write this yourself:

  * `file.url` appears in the API response BEFORE signing has finished. Polling
    for its presence gets you the repackaged-but-unsigned zip. The only honest
    check is whether META-INF/ exists inside the archive, which is what this
    does.
  * Creating an add-on is POST /addons/addon/ ; adding a version to an existing
    one is POST /addons/addon/<id>/versions/ . Hitting the wrong one returns a
    bare 404 with no hint.
  * The JWT must be short-lived (AMO rejects exp more than 60s out) and is
    regenerated per request here for that reason.
"""
import base64, hashlib, hmac, io, json, os, pathlib, re, secrets, sys, time
import urllib.error, urllib.request, zipfile

API = "https://addons.mozilla.org/api/v5"
CREDS = pathlib.Path.home() / ".amo-credentials"


def credentials():
    d = {}
    if CREDS.exists():
        d = dict(re.findall(r"^(\w+)=(.*)$", CREDS.read_text(), re.M))
    iss = os.environ.get("AMO_JWT_ISSUER", d.get("AMO_JWT_ISSUER", "")).strip()
    sec = os.environ.get("AMO_JWT_SECRET", d.get("AMO_JWT_SECRET", "")).strip()
    if not iss or not sec:
        sys.exit(f"no AMO credentials — set AMO_JWT_ISSUER and AMO_JWT_SECRET in {CREDS}")
    if not re.fullmatch(r"user:\d+:\d+", iss):
        sys.exit(f"AMO_JWT_ISSUER looks wrong: expected user:<digits>:<digits>, got {len(iss)} chars.\n"
                 "A duplicated 'user:' prefix is the usual cause of this.")
    return iss, sec


ISS, SEC = credentials()
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")


def token():
    now = int(time.time())
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64(json.dumps({"iss": ISS, "jti": secrets.token_hex(8), "iat": now,
                           "exp": now + 55}, separators=(",", ":")).encode())
    msg = head + b"." + body
    return (msg + b"." + b64(hmac.new(SEC.encode(), msg, hashlib.sha256).digest())).decode()


def call(method, url, data=None, ctype=None, raw=False):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"JWT {token()}")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            b = r.read()
            return r.status, (b if raw else json.loads(b or b"{}"))
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:600].decode("utf-8", "replace")


def main():
    xpi = pathlib.Path(sys.argv[1])
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else xpi.with_name(
        xpi.stem + "-signed.xpi")
    manifest = json.loads(zipfile.ZipFile(xpi).read("manifest.json"))
    ext_id = manifest["browser_specific_settings"]["gecko"]["id"]
    version = manifest["version"]
    print(f"→ {xpi.name}: {ext_id} {version}")

    boundary = "----amo" + secrets.token_hex(12)
    body = f"--{boundary}\r\nContent-Disposition: form-data; name=\"channel\"\r\n\r\nunlisted\r\n".encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"upload\"; "
             f"filename=\"{xpi.name}\"\r\nContent-Type: application/x-xpinstall\r\n\r\n").encode()
    body += xpi.read_bytes() + f"\r\n--{boundary}--\r\n".encode()

    st, res = call("POST", f"{API}/addons/upload/", body,
                   f"multipart/form-data; boundary={boundary}")
    if st not in (200, 201, 202):
        sys.exit(f"✗ upload HTTP {st}: {res}")
    uuid = res["uuid"]

    for _ in range(60):
        time.sleep(5)
        st, res = call("GET", f"{API}/addons/upload/{uuid}/")
        if st == 200 and res.get("processed"):
            break
    else:
        sys.exit("✗ validation never finished")

    val = res.get("validation") or {}
    print(f"  validation: valid={res.get('valid')} errors={val.get('errors')} "
          f"warnings={val.get('warnings')}")
    for m in (val.get("messages") or [])[:10]:
        print(f"    [{m.get('type')}] {m.get('message')}")
    if not res.get("valid"):
        sys.exit("✗ AMO rejected the package")

    # add a version to an existing add-on; fall back to creating it
    st, res = call("POST", f"{API}/addons/addon/{ext_id}/versions/",
                   json.dumps({"upload": uuid}).encode(), "application/json")
    if st == 404:
        print("  add-on not known yet — creating it")
        st, res = call("POST", f"{API}/addons/",
                       json.dumps({"version": {"upload": uuid}}).encode(), "application/json")
        res = res if not isinstance(res, str) else {}
        vid = (res.get("version") or {}).get("id")
    else:
        vid = res.get("id") if isinstance(res, dict) else None
    if not vid:
        sys.exit(f"✗ could not create version: HTTP {st}: {res}")
    print(f"  version id {vid}")

    for i in range(90):
        st, r2 = call("GET", f"{API}/addons/addon/{ext_id}/versions/{vid}/")
        f = (r2 or {}).get("file") or {} if st == 200 else {}
        if f.get("url"):
            st2, blob = call("GET", f["url"], raw=True)
            if st2 == 200 and isinstance(blob, (bytes, bytearray)):
                names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
                # presence of file.url proves nothing; META-INF does
                if any(n.startswith("META-INF/") for n in names):
                    out.write_bytes(blob)
                    print(f"✓ signed: {out} ({len(blob)} bytes)")
                    return
        if i and i % 6 == 0:
            print(f"  waiting for signing… ({i * 10}s)")
        time.sleep(10)
    sys.exit("✗ signing did not complete in time")


if __name__ == "__main__":
    main()
