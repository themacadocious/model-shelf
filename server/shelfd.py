#!/usr/bin/env python3
"""shelfd - receives captured 3D-model projects from the browser extension and
commits them into the Forgejo model-shelf repo.

One directory per model under models/<source>/<id>-<slug>/, holding model.yaml
(the canonical record), README.md (what Forgejo renders), the page snapshot,
images, and the model files themselves via Git LFS.
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml
from flask import Flask, jsonify, request, send_from_directory

# ---------------------------------------------------------------- configuration

WORKDIR = Path(os.environ.get("SHELF_WORKDIR", "/var/lib/shelfd/repo"))
REPO_URL = os.environ["SHELF_REPO_URL"]            # https://git.../duckbill/model-shelf.git
FORGEJO_USER = os.environ.get("SHELF_GIT_USER", "duckbill")
FORGEJO_TOKEN = os.environ["SHELF_FORGEJO_TOKEN"]  # push credential
API_TOKEN = os.environ["SHELF_API_TOKEN"]          # what the extension presents
GIT_NAME = os.environ.get("SHELF_GIT_NAME", "model-shelf")
GIT_EMAIL = os.environ.get("SHELF_GIT_EMAIL", "model-shelf@localhost")
MAX_UPLOAD_MB = int(os.environ.get("SHELF_MAX_UPLOAD_MB", "1024"))
# Signed XPIs and the Firefox update manifest. Served unauthenticated because
# Firefox's updater does not carry our bearer token; the packages are signed,
# so integrity does not depend on this being private.
EXT_DIR = Path(os.environ.get("SHELF_EXT_DIR", "/var/lib/shelfd/ext"))
# Mesh -> STEP conversion via cad-transmogrifier's `stl2step` launcher. It runs
# as a subprocess in its own venv: OpenCASCADE is a heavy dependency shelfd
# itself has no need for. Unset means the feature is off.
STL2STEP = os.environ.get("SHELF_STL2STEP", "")
CONVERT_TIMEOUT = int(os.environ.get("SHELF_CONVERT_TIMEOUT", "600"))  # per file

# Extensions that go to LFS. Images stay in plain git so Forgejo renders them,
# and so a `git clone` of just the metadata is still browsable.
LFS_SUFFIXES = {
    ".stl", ".3mf", ".step", ".stp", ".obj", ".ply", ".f3d", ".f3z", ".iges",
    ".igs", ".zip", ".rar", ".7z", ".gcode", ".bgcode", ".sldprt", ".ipt",
    ".blend", ".dxf", ".amf", ".gltf", ".glb", ".x3d", ".wrl", ".lys", ".form",
}

SOURCES = {
    "printables": "Printables",
    "thingiverse": "Thingiverse",
    "makerworld": "MakerWorld",
    "thangs": "Thangs",
    "cults3d": "Cults3D",
    "youmagine": "YouMagine",
    "github": "GitHub",
    "web": "Web",
}

# what stl2step reads; converted files land in files/step/
MESH_SUFFIXES = {".stl", ".3mf"}
STEP_DIR = "files/step"

STATUSES = ["wishlist", "queued", "printed", "in-use", "failed", "reference"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

_git_lock = threading.Lock()

# ---------------------------------------------------------------------- helpers


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, maxlen: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:maxlen].rstrip("-")) or "untitled"


def safe_name(name: str) -> str:
    """A filename we are willing to write. Strips any path component."""
    name = os.path.basename(name or "").strip()
    name = re.sub(r"[^A-Za-z0-9._()\-\[\]]+", "_", name)
    name = name.lstrip(".") or "file"
    return name[:120]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args, check=True, capture=True):
    return subprocess.run(
        ["git", *args], cwd=WORKDIR, check=check,
        capture_output=capture, text=True, timeout=600,
    )


def authed_remote() -> str:
    # token goes in the remote URL; the repo dir is 0700 and root-owned
    proto, rest = REPO_URL.split("://", 1)
    return f"{proto}://{FORGEJO_USER}:{FORGEJO_TOKEN}@{rest}"


# ------------------------------------------------------------------ repo set-up

def _anycase(suffix: str) -> str:
    """*.[sS][tT][eE][pP]: gitattributes patterns are case-sensitive, and sites
    hand out Orin_case.STEP as readily as bracket.step."""
    return "*." + "".join(f"[{c}{c.upper()}]" if c.isalpha() else c for c in suffix[1:])


GITATTRIBUTES = "\n".join(
    f"{_anycase(suf)} filter=lfs diff=lfs merge=lfs -text" for suf in sorted(LFS_SUFFIXES)
) + "\n"

TAXONOMY_SEED = {
    "tags": {
        "bracket": "Mounts and brackets",
        "enclosure": "Cases and enclosures",
        "adapter": "Adapters and couplers",
        "organizer": "Storage, bins, holders",
        "tool": "Jigs, tools, fixtures",
        "printer-mod": "3D printer upgrades",
        "homelab": "Rack, network, server hardware",
        "automotive": "Car and vehicle parts",
        "electronics": "PCB holders, panel mounts",
        "household": "Repairs and household parts",
        "toy": "Toys, games, models",
        "functional": "Parts that do a job",
        "decorative": "Looks over function",
    },
    "projects": {
        "ugb-gauge": "Garage Tinkering UGB round CAN display",
        "crosstrek": "2018 Crosstrek head unit / gauge",
        "entryway-panel": "65in entryway kiosk",
        "dialpanel01": "Round LCD HA control panel",
        "homelab-rack": "Rack and network hardware",
        "orin-nano": "Jetson Orin Nano 360 car view",
    },
    "statuses": STATUSES,
}


def ensure_repo():
    """Clone once, with LFS smudge off so we never pull down model files we
    did not just create. Keeps the working copy to text + pointers."""
    WORKDIR.parent.mkdir(parents=True, exist_ok=True)
    if not (WORKDIR / ".git").exists():
        if WORKDIR.exists():
            shutil.rmtree(WORKDIR)
        env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
        subprocess.run(
            ["git", "clone", authed_remote(), str(WORKDIR)],
            check=True, capture_output=True, text=True, env=env, timeout=600,
        )
    git("config", "user.name", GIT_NAME)
    git("config", "user.email", GIT_EMAIL)
    git("config", "lfs.fetchexclude", "*")
    git("config", "remote.origin.url", authed_remote())
    # the LFS filters are per clone unless installed globally; without them a
    # re-cloned shelf would commit meshes and STEP files as plain blobs
    if git("config", "filter.lfs.clean", check=False).returncode != 0:
        git("lfs", "install", "--local")
    # empty repo: make the first commit
    if git("rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        seed_repo()


def seed_repo():
    (WORKDIR / "models").mkdir(exist_ok=True)
    (WORKDIR / ".gitattributes").write_text(GITATTRIBUTES)
    (WORKDIR / "taxonomy.yml").write_text(
        yaml.safe_dump(TAXONOMY_SEED, sort_keys=False, allow_unicode=True))
    (WORKDIR / "models" / ".gitkeep").write_text("")
    (WORKDIR / "README.md").write_text(SHELF_README)
    reindex()
    git("add", "-A")
    git("commit", "-m", "Initialise model shelf")
    git("branch", "-M", "main")
    git("push", "-u", "origin", "main")


SHELF_README = """# Model shelf

Captured 3D model projects — the page, the metadata, the licence and the files —
saved from the browser so they can be found again later.

- **[index.md](index.md)** — everything, newest first
- **[by-project.md](by-project.md)** — grouped by the thing you were building
- **[by-tag.md](by-tag.md)** — grouped by what kind of part it is
- **[taxonomy.yml](taxonomy.yml)** — the controlled vocabulary for `tags:` and `projects:`

Each model lives in `models/<source>/<id>-<slug>/`:

| file | what it is |
| --- | --- |
| `model.yaml` | the canonical record — source, author, licence, tags, file checksums |
| `README.md` | generated summary, what Forgejo shows when you open the folder |
| `NOTES.md` | yours to edit: print settings, what you used it for, what went wrong |
| `files/` | the model files, stored via Git LFS |
| `images/` | cover and gallery images, plain git so they render |
| `page.html` | snapshot of the source page as it looked when captured |

Model files are in **Git LFS**. A plain `git clone` gives you all the metadata
and images with pointer files in place of the meshes; run `git lfs pull` (or
`git lfs pull --include="models/printables/123*"`) to fetch the real thing.
"""

# ------------------------------------------------------------------- the record


def model_dir(source: str, mid: str, title: str) -> Path:
    return WORKDIR / "models" / source / f"{mid}-{slugify(title)}"


def find_existing(source: str, mid: str) -> Path | None:
    base = WORKDIR / "models" / source
    if not base.is_dir():
        return None
    for d in base.iterdir():
        if d.is_dir() and d.name.split("-")[0] == mid:
            return d
    return None


def load_model(d: Path) -> dict:
    try:
        return yaml.safe_load((d / "model.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def write_model(d: Path, rec: dict):
    (d / "model.yaml").write_text(
        yaml.safe_dump(rec, sort_keys=False, allow_unicode=True, width=100))
    (d / "README.md").write_text(render_model_readme(rec, d))


def render_model_readme(rec: dict, d: Path) -> str:
    lines = [f"# {rec.get('title', 'Untitled')}", ""]
    cover = next((i["path"] for i in rec.get("images", []) if i.get("cover")),
                 next((i["path"] for i in rec.get("images", [])), None))
    if cover:
        lines += [f"![cover]({cover})", ""]
    author = rec.get("author") or {}
    lic = rec.get("license") or {}
    meta = [
        ("Source", f"[{SOURCES.get(rec.get('source'), rec.get('source'))}]({rec.get('source_url')})"),
        ("Author", f"[{author.get('name')}]({author['url']})" if author.get("url")
         else author.get("name")),
        ("Licence", f"[{lic.get('name')}]({lic['url']})" if lic.get("url") else lic.get("name")),
        ("Status", rec.get("status")),
        ("Tags", ", ".join(f"`{t}`" for t in rec.get("tags", []))),
        ("Projects", ", ".join(f"`{p}`" for p in rec.get("projects", []))),
        ("Captured", rec.get("captured")),
    ]
    lines += ["| | |", "| --- | --- |"]
    lines += [f"| **{k}** | {v} |" for k, v in meta if v]
    lines += [""]
    if rec.get("summary"):
        lines += [rec["summary"].strip(), ""]
    files = rec.get("files", [])
    if files:
        lines += ["## Files", "", "| file | size | sha256 |", "| --- | --- | --- |"]
        for f in files:
            lines.append(f"| [{Path(f['path']).name}]({f['path']}) | "
                         f"{human_size(f.get('bytes', 0))} | `{f.get('sha256', '')[:12]}` |")
        lines += ["", "```bash", f"git lfs pull --include=\"{rel(d)}/files/*\"", "```", ""]
    else:
        lines += ["## Files", "",
                  "_No model files captured._ Attach a download later with the "
                  "extension's **Attach files** button, or drop a zip into `files/`.", ""]
    if (d / "NOTES.md").exists():
        lines += ["## Notes", "", (d / "NOTES.md").read_text().strip(), ""]
    return "\n".join(lines)


def rel(p: Path) -> str:
    return str(p.relative_to(WORKDIR))


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


# ------------------------------------------------------------------- the index


def all_models() -> list[tuple[Path, dict]]:
    out = []
    base = WORKDIR / "models"
    for src in sorted(base.iterdir()) if base.is_dir() else []:
        if not src.is_dir():
            continue
        for d in sorted(src.iterdir()):
            if (d / "model.yaml").exists():
                out.append((d, load_model(d)))
    return out


def _row(d: Path, rec: dict) -> str:
    src = SOURCES.get(rec.get("source"), rec.get("source", ""))
    nfiles = len(rec.get("files", []))
    return (f"| [{rec.get('title', d.name)}]({rel(d)}) "
            f"| [{src}]({rec.get('source_url', '')}) "
            f"| {rec.get('status', '')} "
            f"| {' '.join('`' + t + '`' for t in rec.get('tags', []))} "
            f"| {' '.join('`' + p + '`' for p in rec.get('projects', []))} "
            f"| {nfiles or '—'} "
            f"| {(rec.get('captured') or '')[:10]} |")


HEADER = ("| model | source | status | tags | projects | files | captured |\n"
          "| --- | --- | --- | --- | --- | --- | --- |")


def reindex():
    models = all_models()
    models.sort(key=lambda t: t[1].get("captured", ""), reverse=True)

    lines = [f"# Index — {len(models)} models", "",
             f"_Regenerated {now_iso()}._", "", HEADER]
    lines += [_row(d, r) for d, r in models]
    (WORKDIR / "index.md").write_text("\n".join(lines) + "\n")

    for key, fname, title in (("projects", "by-project.md", "By project"),
                              ("tags", "by-tag.md", "By tag")):
        groups: dict[str, list] = {}
        for d, r in models:
            for v in (r.get(key) or ["(none)"]):
                groups.setdefault(v, []).append((d, r))
        out = [f"# {title}", ""]
        for name in sorted(groups, key=lambda k: (k == "(none)", k)):
            out += [f"## {name} ({len(groups[name])})", "", HEADER]
            out += [_row(d, r) for d, r in groups[name]]
            out += [""]
        (WORKDIR / fname).write_text("\n".join(out) + "\n")


# ------------------------------------------------------------- STEP conversion
#
# A conversion target is a mesh file in files/, or a mesh inside a zip there
# (Thingiverse only hands out zips), named "files/x.zip::inner/part.stl". The
# result goes to files/step/<name>.step and is listed in model.yaml's files
# with `converted_from` pointing back at the target.


def zip_meshes(data: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return []
    return sorted(n for n in names
                  if Path(n).suffix.lower() in MESH_SUFFIXES
                  and not n.endswith("/") and "__MACOSX" not in n
                  and not Path(n).name.startswith("."))


LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


def file_bytes(path: Path) -> bytes:
    """The real contents of a file in the working copy. The clone never smudges
    LFS files, so anything not written by this process is a pointer: fetch just
    that object and read it from the LFS store, leaving the work tree alone."""
    data = path.read_bytes()
    if not data.startswith(LFS_POINTER):
        return data
    m = re.search(rb"^oid sha256:([0-9a-f]{64})$", data, re.M)
    if not m:
        raise ValueError(f"{rel(path)}: unreadable LFS pointer")
    oid = m.group(1).decode()
    obj = WORKDIR / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    if not obj.exists():
        git("-c", "lfs.fetchexclude=", "lfs", "fetch", "origin", "-I", rel(path))
    if not obj.exists():
        raise ValueError(f"{rel(path)}: LFS object {oid[:12]} not on the server")
    return obj.read_bytes()


def convertible(d: Path, rec: dict) -> list[dict]:
    """Every mesh this model has, and the STEP file already made from it, if any.
    Zips saved before zip contents were recorded are opened here instead."""
    done = {f["converted_from"]: f["path"] for f in rec.get("files", [])
            if f.get("converted_from")}
    out = []
    for f in rec.get("files", []):
        if f.get("converted_from"):
            continue
        suffix = Path(f["path"]).suffix.lower()
        if suffix in MESH_SUFFIXES:
            names = [f["path"]]
        elif suffix == ".zip":
            meshes = f.get("meshes")
            if meshes is None:
                try:
                    meshes = zip_meshes(file_bytes(d / f["path"]))
                except (OSError, ValueError, subprocess.CalledProcessError):
                    meshes = []
            names = [f"{f['path']}::{m}" for m in meshes]
        else:
            continue
        out += [{"source": n, "name": n.split("::")[-1].rsplit("/", 1)[-1],
                 "step": done.get(n)} for n in names]
    return out


def mesh_bytes(d: Path, target: str) -> bytes:
    path, _, member = target.partition("::")
    data = file_bytes(d / path)
    if not member:
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.read(member)


def step_name(target: str, taken: set[str]) -> str:
    stem = Path(safe_name(target.split("::")[-1])).stem or "part"
    name, n = f"{stem}.step", 2
    while name in taken:
        name, n = f"{stem}-{n}.step", n + 1
    return name


def run_stl2step(src: Path, out: Path) -> tuple[bool, str]:
    try:
        p = subprocess.run([STL2STEP, str(src), "-o", str(out)],
                           capture_output=True, text=True, timeout=CONVERT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"gave up after {CONVERT_TIMEOUT} s"
    except OSError as exc:
        return False, f"could not run stl2step: {exc}"
    log = (p.stderr or "").strip()
    if p.returncode == -9:  # SIGKILL: in practice the cgroup's memory limit
        return False, ("killed, most likely out of memory: organic meshes can need "
                       "several GB (see MemoryMax in the README)\n" + log[-400:])
    if p.returncode != 0 or not out.is_file() or not out.stat().st_size:
        return False, log[-600:] or f"stl2step exited {p.returncode}"
    return True, log[-600:]


# ------------------------------------------------------------------------- auth


def authorised() -> bool:
    hdr = request.headers.get("Authorization", "")
    return hdr.startswith("Bearer ") and hdr[7:].strip() == API_TOKEN


@app.before_request
def _guard():
    if request.path == "/api/health" or request.path.startswith("/ext/"):
        return None
    if request.method == "OPTIONS":
        return None
    if not authorised():
        return jsonify(error="unauthorised"), 401
    return None


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ----------------------------------------------------------------------- routes


@app.get("/ext/<path:name>")
def ext(name):
    """Extension self-hosting: updates.json plus the signed .xpi files."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return jsonify(error="bad name"), 400
    if not (EXT_DIR / name).is_file():
        return jsonify(error="not found"), 404
    mime = ("application/x-xpinstall" if name.endswith(".xpi")
            else "application/json" if name.endswith(".json") else None)
    return send_from_directory(EXT_DIR, name, mimetype=mime)


@app.get("/api/health")
def health():
    return jsonify(ok=True, service="shelfd", repo=REPO_URL,
                   convert_available=bool(STL2STEP))


@app.get("/api/taxonomy")
def taxonomy():
    with _git_lock:
        ensure_repo()
        tx = yaml.safe_load((WORKDIR / "taxonomy.yml").read_text()) or {}
        used_tags, used_projects = set(), set()
        for _, r in all_models():
            used_tags.update(r.get("tags") or [])
            used_projects.update(r.get("projects") or [])
    tx["tags"] = {**{t: "" for t in sorted(used_tags)}, **(tx.get("tags") or {})}
    tx["projects"] = {**{p: "" for p in sorted(used_projects)},
                      **(tx.get("projects") or {})}
    return jsonify(tx)


@app.get("/api/lookup")
def lookup():
    source = request.args.get("source", "")
    mid = str(request.args.get("id", ""))
    with _git_lock:
        ensure_repo()
        d = find_existing(source, mid) if source and mid else None
        if not d:
            return jsonify(found=False)
        rec = load_model(d)
    return jsonify(found=True, path=rel(d), record=rec,
                   url=f"{REPO_URL[:-4]}/src/branch/main/{rel(d)}")


@app.post("/api/save")
def save():
    try:
        payload = json.loads(request.form["payload"])
    except (KeyError, json.JSONDecodeError) as exc:
        return jsonify(error=f"bad payload: {exc}"), 400

    source = payload.get("source") or "web"
    mid = str(payload.get("id") or "").strip()
    title = payload.get("title") or "Untitled"
    if source not in SOURCES:
        source = "web"
    if not mid:
        mid = hashlib.sha1((payload.get("source_url") or title).encode()).hexdigest()[:10]

    with _git_lock:
        ensure_repo()
        sync()

        d = find_existing(source, mid)
        fresh = d is None
        if fresh:
            d = model_dir(source, mid, title)
        d.mkdir(parents=True, exist_ok=True)
        (d / "files").mkdir(exist_ok=True)
        (d / "images").mkdir(exist_ok=True)

        rec = load_model(d) if not fresh else {}
        rec.update({
            "schema": 1,
            "id": f"{source}:{mid}",
            "source": source,
            "source_url": payload.get("source_url"),
            "title": title,
            "author": payload.get("author") or {},
            "license": payload.get("license") or {},
            "published": payload.get("published"),
            "summary": payload.get("summary"),
            "stats": payload.get("stats") or {},
            "updated": now_iso(),
        })
        rec.setdefault("captured", now_iso())
        # user-editable fields: the extension wins when it sends them,
        # otherwise whatever is already on disk survives a re-capture
        for field, default in (("tags", []), ("projects", []),
                               ("status", "wishlist")):
            if payload.get(field) is not None:
                rec[field] = payload[field]
            else:
                rec.setdefault(field, default)

        if payload.get("page_html"):
            (d / "page.html").write_text(payload["page_html"][:4_000_000])
        if payload.get("page_text"):
            (d / "page.md").write_text(payload["page_text"][:1_000_000])
        if not (d / "NOTES.md").exists():
            (d / "NOTES.md").write_text(
                "<!-- print settings, what you used it for, what went wrong -->\n")

        files = rec.get("files", [])
        images = rec.get("images", [])
        meta_by_field = {m.get("field"): m for m in (payload.get("uploads") or [])}

        for field, storage in request.files.lists():
            for fs in storage:
                data = fs.read()
                if not data:
                    continue
                meta = meta_by_field.get(field, {})
                name = safe_name(meta.get("name") or fs.filename or field)
                kind = "images" if field.startswith("image") else "files"
                dest = d / kind / name
                dest.write_bytes(data)
                entry = {
                    "path": f"{kind}/{name}",
                    "bytes": len(data),
                    "sha256": sha256_bytes(data),
                }
                if kind == "images":
                    entry["source_url"] = meta.get("source_url")
                    entry["cover"] = bool(meta.get("cover"))
                    images = [i for i in images if i["path"] != entry["path"]]
                    images.append(entry)
                else:
                    entry["lfs"] = dest.suffix.lower() in LFS_SUFFIXES
                    entry["source_url"] = meta.get("source_url")
                    if dest.suffix.lower() == ".zip":
                        entry["meshes"] = zip_meshes(data)
                    files = [f for f in files if f["path"] != entry["path"]]
                    files.append(entry)

        rec["files"] = sorted(files, key=lambda f: f["path"])
        rec["images"] = sorted(images, key=lambda i: (not i.get("cover"), i["path"]))
        rec["capture"] = payload.get("capture") or {}
        rec["capture"]["files_ok"] = bool(rec["files"])

        write_model(d, rec)
        reindex()

        verb = "Add" if fresh else "Update"
        msg = (f"{verb} {SOURCES.get(source, source)}: {title}\n\n"
               f"{rec['source_url']}\n"
               f"{len(rec['files'])} file(s), {len(rec['images'])} image(s)")
        git("add", "-A")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return jsonify(ok=True, changed=False, path=rel(d),
                           url=web_url(d), files=len(rec["files"]),
                           convert_available=bool(STL2STEP),
                           convertible=convertible(d, rec))
        git("commit", "-m", msg)
        push = git("push", "origin", "HEAD:main", check=False)
        if push.returncode != 0:
            return jsonify(error="push failed", detail=push.stderr[-2000:]), 500

        conv = convertible(d, rec)

    return jsonify(ok=True, changed=True, path=rel(d), url=web_url(d),
                   files=len(rec["files"]), created=fresh,
                   convert_available=bool(STL2STEP), convertible=conv)


@app.get("/api/convertible")
def convertible_route():
    source, mid = request.args.get("source", ""), str(request.args.get("id", ""))
    with _git_lock:
        ensure_repo()
        d = find_existing(source, mid) if source and mid else None
        if not d:
            return jsonify(error="not found"), 404
        items = convertible(d, load_model(d))
    return jsonify(available=bool(STL2STEP), files=items)


@app.post("/api/convert")
def convert():
    """Convert some or all of a saved model's meshes to STEP and commit them.
    `files` is "all" or a list of sources as /api/convertible names them.
    The converter runs outside the git lock: a big mesh can take a minute."""
    if not STL2STEP:
        return jsonify(error="STEP conversion is not set up (SHELF_STL2STEP)"), 501
    body = request.get_json(force=True, silent=True) or {}
    source, mid = body.get("source"), str(body.get("id", ""))
    wanted = body.get("files", "all")
    if wanted != "all" and not (isinstance(wanted, list)
                                and all(isinstance(t, str) for t in wanted)):
        return jsonify(error='files must be "all" or a list of names'), 400

    with tempfile.TemporaryDirectory(prefix="shelfd-step-") as tmp:
        tmp = Path(tmp)
        results, jobs = [], []
        with _git_lock:
            ensure_repo()
            sync()
            d = find_existing(source, mid)
            if not d:
                return jsonify(error="not found"), 404
            known = [c["source"] for c in convertible(d, load_model(d))]
            targets = known if wanted == "all" else [t for t in wanted if t in known]
            results += [{"source": t, "ok": False, "error": "not a mesh in this model"}
                        for t in ([] if wanted == "all" else wanted) if t not in known]
            for i, t in enumerate(targets):
                src = tmp / str(i) / safe_name(t.split("::")[-1])
                src.parent.mkdir()
                try:
                    src.write_bytes(mesh_bytes(d, t))
                except (OSError, ValueError, KeyError, zipfile.BadZipFile,
                        subprocess.CalledProcessError) as exc:
                    results.append({"source": t, "ok": False, "error": f"could not read: {exc}"})
                    continue
                jobs.append((t, src))

        done = []
        for t, src in jobs:
            out = src.with_suffix(".step")
            ok, log = run_stl2step(src, out)
            results.append({"source": t, "ok": ok, ("log" if ok else "error"): log})
            if ok:
                done.append((t, out))

        if not results:
            return jsonify(error="no STL or 3MF files to convert"), 400
        if not done:
            return jsonify(ok=True, changed=False, results=results)

        with _git_lock:
            sync()
            d = find_existing(source, mid)
            if not d:
                return jsonify(error="model vanished while converting"), 409
            rec = load_model(d)
            files = rec.get("files", [])
            (d / STEP_DIR).mkdir(parents=True, exist_ok=True)
            for t, out in done:
                old = next((f for f in files if f.get("converted_from") == t), None)
                if old:
                    path = old["path"]
                else:
                    taken = {Path(f["path"]).name for f in files
                             if f["path"].startswith(STEP_DIR + "/")}
                    path = f"{STEP_DIR}/{step_name(t, taken)}"
                data = out.read_bytes()
                (d / path).write_bytes(data)
                files = [f for f in files if f["path"] != path]
                files.append({"path": path, "bytes": len(data), "sha256": sha256_bytes(data),
                              "lfs": True, "converted_from": t, "converted": now_iso()})
                next(r for r in results if r["source"] == t)["path"] = path
            rec["files"] = sorted(files, key=lambda f: f["path"])
            rec["updated"] = now_iso()
            write_model(d, rec)
            reindex()
            git("add", "-A")
            if git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return jsonify(ok=True, changed=False, results=results, path=rel(d),
                               url=web_url(d))
            made = [f"{r['source']} -> {r['path']}" for r in results if r.get("path")]
            git("commit", "-m", f"Convert to STEP: {rec.get('title', d.name)}\n\n"
                + "\n".join(made))
            push = git("push", "origin", "HEAD:main", check=False)
            if push.returncode != 0:
                return jsonify(error="push failed", detail=push.stderr[-2000:]), 500

    return jsonify(ok=True, changed=True, results=results, path=rel(d), url=web_url(d))


@app.post("/api/annotate")
def annotate():
    """Update tags/projects/status or append a note without re-capturing."""
    body = request.get_json(force=True, silent=True) or {}
    source, mid = body.get("source"), str(body.get("id", ""))
    with _git_lock:
        ensure_repo()
        sync()
        d = find_existing(source, mid)
        if not d:
            return jsonify(error="not found"), 404
        rec = load_model(d)
        for field in ("tags", "projects", "status"):
            if body.get(field) is not None:
                rec[field] = body[field]
        if body.get("note"):
            with (d / "NOTES.md").open("a") as fh:
                fh.write(f"\n### {now_iso()[:10]}\n\n{body['note'].strip()}\n")
        rec["updated"] = now_iso()
        write_model(d, rec)
        reindex()
        git("add", "-A")
        if git("diff", "--cached", "--quiet", check=False).returncode != 0:
            git("commit", "-m", f"Annotate {rec.get('title', d.name)}")
            git("push", "origin", "HEAD:main")
    return jsonify(ok=True, path=rel(d), url=web_url(d))


def web_url(d: Path) -> str:
    return f"{REPO_URL.removesuffix('.git')}/src/branch/main/{rel(d)}"


def sync():
    """Only this service writes to the repo, but you may well edit NOTES.md in
    the Forgejo UI — so take remote as truth for anything we are not about to
    write, and never leave a conflicted tree behind."""
    git("fetch", "origin", "main", check=False)
    if git("rev-parse", "--verify", "origin/main", check=False).returncode == 0:
        git("reset", "--hard", "origin/main", check=False)
        git("clean", "-fd", check=False)


def main():
    with _git_lock:
        ensure_repo()
        sync()
    from waitress import serve
    serve(app, host=os.environ.get("SHELF_BIND", "0.0.0.0"),
          port=int(os.environ.get("SHELF_PORT", "8710")),
          max_request_body_size=MAX_UPLOAD_MB * 1024 * 1024,
          threads=4)


if __name__ == "__main__":
    main()
