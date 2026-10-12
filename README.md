# Model Shelf

Click a button on a Printables, Thingiverse, MakerWorld or Thangs page and the
project — the page, the metadata, the licence and the model files — lands in a
Git repository, catalogued so you can find it again.

```
Firefox extension  ──POST──▶  shelfd  ──git push──▶  your repo
   reads as you                commits + reindexes    files via Git LFS
```

The page is read **in the browser, as you**. That is the whole reason this is an
extension and not a server-side scraper: Printables sits behind Cloudflare and
Thingiverse hides downloads behind a login, and neither is a problem for a
browser that is already signed in.

Built against a self-hosted [Forgejo](https://forgejo.org/), but nothing depends
on that — any Git remote that speaks HTTPS and LFS will do.

## How this was built

**This project was written by an AI.** The code, documentation and commit
messages were produced by Claude (Anthropic) in an interactive session,
directed and tested by the repository owner. It runs in production on that
homelab — the site adapters were verified against live pages and the capture
path tested end to end — but it has not had independent human code review.
Read it with that in mind.

## Why a shelf and not bookmarks

A bookmark rots. The model gets delisted, the author deletes their account, the
site changes its licence terms, or you simply cannot remember whether the
bracket you printed two years ago came from Thingiverse or Printables. This
keeps the files, the licence you were granted at the time, and your own notes
about how the print actually went.

## Cataloguing

One directory per model, `models/<source>/<id>-<slug>/`. The id comes first so
the path stays stable when a title changes.

```
models/thingiverse/763622-3dbenchy/
  model.yaml   canonical record — source, author, licence, checksums, tags
  README.md    generated; what a Git forge renders when you open the folder
  NOTES.md     yours — print settings, what you used it for, what went wrong
  files/       model files, via Git LFS
  images/      cover + gallery, plain git so they render in the web UI
  page.html    snapshot of the page as it was
  page.md      readable text version
```

**Tags over folders.** A model is one directory but carries many tags and
projects, which is the only way "what did I use for the gauge dock?" and "what
brackets do I have?" can both be answered. The vocabulary lives in
`taxonomy.yml`; the popup offers what is already in use plus anything you type.

Three views are regenerated on every save: `index.md` (everything, newest
first), `by-project.md` and `by-tag.md`. Plus `grep` over a clone, and your
forge's own code search.

`status:` is `wishlist | queued | printed | in-use | failed | reference`. The
difference between "I bookmarked this" and "I printed this and it worked" is
what you actually want to know six months later.

Re-saving a page **updates** its entry rather than duplicating it. Tags,
projects, status and `NOTES.md` set earlier are loaded back into the popup
first, so a re-save never silently blanks them.

## Install

### 1. The service

Needs Python 3.11+, `git` and `git-lfs`, and push access to an empty repo.

```bash
python3 -m venv /opt/shelfd/venv
/opt/shelfd/venv/bin/pip install -r server/requirements.txt
install -m 644 server/shelfd.py /opt/shelfd/shelfd.py
install -m 644 server/shelfd.service.example /etc/systemd/system/shelfd.service
```

Write `/etc/shelfd/shelfd.env` (mode 0640, not world-readable):

```ini
SHELF_REPO_URL=https://git.example.com/you/model-shelf.git
SHELF_GIT_USER=you
SHELF_FORGEJO_TOKEN=<a token that can push to that repo>
SHELF_API_TOKEN=<invent one: openssl rand -hex 24>
SHELF_WORKDIR=/var/lib/shelfd/repo
SHELF_PORT=8710
```

Optionally, for **STEP conversion**, add `SHELF_STL2STEP` (see below).

Then `systemctl enable --now shelfd`. On first run it clones the repo, writes
`.gitattributes` for LFS, seeds `taxonomy.yml` and pushes an initial commit.

Put it behind a reverse proxy with TLS. Firefox will not send a bearer token to
an untrusted origin, and you do not want this on plain HTTP anyway.

### Optional: convert meshes to STEP

With [cad-transmogrifier](https://github.com/themacadocious/cad-transmogrifier)
installed, the popup offers to turn a model's STL and 3MF files into editable
STEP solids once they are on the shelf, including the meshes inside a
downloaded zip. Pick some or all; each one lands in `files/step/` next to the
originals, listed in `model.yaml` with `converted_from` naming its mesh, and
is committed like any other save.

It runs as a subprocess in its own venv, so OpenCASCADE never enters shelfd's.
Install it outside `/home`: the unit file sets `ProtectHome=true`.

```bash
git clone https://github.com/themacadocious/cad-transmogrifier.git /opt/cad-transmogrifier
python3 -m venv /opt/cad-transmogrifier/.venv
/opt/cad-transmogrifier/.venv/bin/pip install -r /opt/cad-transmogrifier/requirements.txt
```

```ini
SHELF_STL2STEP=/opt/cad-transmogrifier/transmogrify
SHELF_CONVERT_TIMEOUT=600   # seconds per file; big organic meshes are slow
```

Without `SHELF_STL2STEP` the option simply does not appear.

### 2. The extension

```bash
cp build.env.example build.env   # set your own EXT_ID
./build.sh                       # -> dist/model-shelf-X.Y.Z.xpi
```

Firefox will not *permanently* install an unsigned extension, so you have two
options:

**Just trying it out** — load `dist/…xpi` from `about:debugging` →
This Firefox → Load Temporary Add-on. No account needed. It disappears when
Firefox restarts.

**Keeping it** — sign it through AMO's unlisted channel (see below), then open
the signed `.xpi` in Firefox.

Either way, in the extension's options: set the endpoint, paste
`SHELF_API_TOKEN`, and press **Grant site access**.

> ⚠️ **Firefox MV3 does not grant host permissions automatically.** Without
> pressing that button the extension cannot read a model page at all. This is
> the single most common reason a fresh install appears broken.

## Signing it yourself

"Unlisted" means Mozilla signs the package for you but publishes nothing: it
never appears in the add-ons directory, gets no human review, and nobody can
find it. It is just how Firefox is told that a self-distributed add-on is
allowed to install permanently. The signature is not tied to your machine, so
you can hand the signed file to anyone.

**Getting API credentials**, once:

1. Sign in at [addons.mozilla.org](https://addons.mozilla.org/) — any Firefox
   account works, you do not need to have published anything.
2. Go to **[Manage API Keys](https://addons.mozilla.org/developers/addon/api/key/)**
   (Developer Hub → Tools → Manage API Keys).
3. **Generate new credentials.** You get a **JWT issuer** that looks like
   `user:12345678:123`, and a **JWT secret** of 64 hex characters.
4. The secret is shown **once**. Copy it now.

Put them in `~/.amo-credentials`:

```bash
printf 'AMO_JWT_ISSUER=user:12345678:123\nAMO_JWT_SECRET=abc…\n' > ~/.amo-credentials
chmod 600 ~/.amo-credentials
```

> ⚠️ Copy the issuer carefully. Pasting it after the label, giving you
> `user:user:12345678:123`, is easy to do and produces a confusing failure.
> `tools/amo-sign.py` checks the shape and says so rather than letting AMO
> reject it obscurely.

Then:

```bash
./build.sh && tools/amo-sign.py dist/model-shelf-0.4.0.xpi
```

It uploads, waits for validation, creates the add-on (first run) or adds a
version (later runs), waits for signing, and writes `…-signed.xpi`. Expect
two to five minutes, most of it waiting on Mozilla.

Your `EXT_ID` must be unique to you — it is what AMO signs against, and it
cannot be changed later without breaking updates for anyone who installed it.
Use a domain you control, e.g. `model-shelf@yourdomain.tld`.

### The three secrets in this project, which are easy to confuse

| Secret | Who issues it | Used for | Lives in |
| --- | --- | --- | --- |
| `SHELF_API_TOKEN` | **you invent it** (`openssl rand -hex 24`) | extension → shelfd | `shelfd.env` + extension options |
| Git push token | your Git forge | shelfd → your repo | `shelfd.env` only |
| AMO JWT issuer + secret | addons.mozilla.org | signing the extension | `~/.amo-credentials` only |

None of them belong in this repository, and nothing here reads a credential
from a tracked file.

## Self-hosted updates

Unlisted add-ons do not auto-update unless you host an update manifest. Set
`UPDATE_URL` in `build.env`, then serve `updates.json` and the signed `.xpi`
from `shelfd`'s `/ext/` route (unauthenticated on purpose — Firefox's updater
carries no bearer token; the packages are signed, so integrity does not depend
on that route being private):

```json
{ "addons": { "your@ext.id": { "updates": [
  { "version": "0.4.0",
    "update_link": "https://shelf.example.com/ext/model-shelf-0.4.0.xpi" } ] } } }
```

Drop both in `SHELF_EXT_DIR` (default `/var/lib/shelfd/ext`). The link must be
HTTPS.

## Site support

| Site | Metadata | Files |
| --- | --- | --- |
| Printables | public GraphQL API | ✅ via `getDownloadLink` |
| Thingiverse | DOM | ✅ `/thing:<id>/zip`, needs you signed in |
| MakerWorld | DOM + API | best-effort |
| Thangs, Cults3D | DOM | ✗ no usable endpoint |
| anything else | JSON-LD + OpenGraph | ✗ |

When files cannot be fetched the capture still succeeds: the record is written
with `capture.files_ok: false` and the reason recorded in `model.yaml` and shown
in the popup. Nothing is ever silently half-saved.

### Things learned the hard way

- **Thingiverse's `og:` tags are site-wide boilerplate**, identical on every
  page. Scraping them archives the same generic blurb for every model. Its DOM
  has the real values.
- **A login wall answers `200` with HTML.** The downloader checks
  `content-type` and refuses to store an HTML page as a `.zip`, because a
  capture that *looks* successful is worse than one that failed.
- **Printables' GraphQL root field is `print`, not `model`** — and its errors
  arrive in `errors[]` on an HTTP 400, so a client that throws on status alone
  discards the explanation.
- **Downloads must not run in the page.** Page-context fetches are CORS-bound;
  `files.printables.com` sends no CORS headers, and the failure surfaces only as
  a bare `NetworkError`. They run in the background script, which has host
  permissions and is not CORS-bound.

## API

All routes need `Authorization: Bearer $SHELF_API_TOKEN` except `/api/health`
and `/ext/*`.

| Route | Does |
| --- | --- |
| `GET /api/health` | liveness |
| `GET /api/taxonomy` | known tags/projects/statuses, including ones in use |
| `GET /api/lookup?source=&id=` | is this already on the shelf? |
| `POST /api/save` | multipart: `payload` JSON + `file*` / `image*` parts |
| `POST /api/annotate` | change tags/projects/status, or append to NOTES.md |
| `GET /api/convertible?source=&id=` | the model's STL/3MF files (zip contents too) and any STEP already made |
| `POST /api/convert` | JSON `source`, `id`, `files`: `"all"` or a list from `/api/convertible` |

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r server/requirements-dev.txt
.venv/bin/python -m pytest server/tests
# also run one conversion through the real converter:
SHELF_TEST_STL2STEP=/opt/cad-transmogrifier/transmogrify .venv/bin/python -m pytest server/tests
```

## Operating it

`sync()` treats `origin/main` as truth before every write, so editing `NOTES.md`
in your forge's web UI is safe. A local edit in the service's working copy is
not — it will be discarded.

Getting files back out:

```bash
git clone <your repo>
git -C model-shelf lfs pull --include="models/thingiverse/763622-*/files/*"
```

A plain clone gives you all metadata and images with LFS pointers standing in
for the meshes, which keeps a metadata-only checkout small.

## Licensing of what you capture

This tool stores whatever licence the source page declared, at the time you
captured it, in `model.yaml`. It does not check it, and saving a model grants
you nothing. Plenty of popular models are non-commercial or no-derivatives;
that is between you and the author.

## Licence

MIT — see [LICENSE](LICENSE).
