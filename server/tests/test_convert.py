"""STEP conversion in shelfd, end to end against a local bare repo with LFS.

The converter is a stand-in script by default, so these run in a second with
no OpenCASCADE around. Point SHELF_TEST_STL2STEP at a real cad-transmogrifier
`stl2step` launcher to also run test_real_converter.
"""

import importlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# writes a stand-in STEP; any input containing BROKEN fails like stl2step does
FAKE_STL2STEP = """#!/usr/bin/env python3
import sys
from pathlib import Path
src, out = Path(sys.argv[1]), Path(sys.argv[3])
data = src.read_bytes()
if b"BROKEN" in data:
    print(f"{src}: ERROR: mesh is broken", file=sys.stderr)
    sys.exit(1)
out.write_text(f"ISO-10303-21;\\n/* {src.name} {len(data)} */\\nEND-ISO-10303-21;\\n")
print(f"  wrote {out}", file=sys.stderr)
"""


def run(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def load_shelfd(tmp_path, monkeypatch, converter):
    remote = tmp_path / "remote.git"
    if not remote.exists():
        run("git", "init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    monkeypatch.setenv("SHELF_REPO_URL", f"file://{remote}")
    monkeypatch.setenv("SHELF_GIT_USER", "test")
    monkeypatch.setenv("SHELF_FORGEJO_TOKEN", "unused")
    monkeypatch.setenv("SHELF_API_TOKEN", TOKEN)
    monkeypatch.setenv("SHELF_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("SHELF_STL2STEP", converter)
    sys.modules.pop("shelfd", None)
    shelfd = importlib.import_module("shelfd")
    # a file:// URL has nowhere to put credentials
    monkeypatch.setattr(shelfd, "authed_remote", lambda: shelfd.REPO_URL)
    return shelfd


@pytest.fixture
def fake_converter(tmp_path):
    p = tmp_path / "stl2step"
    p.write_text(FAKE_STL2STEP)
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def shelf(tmp_path, monkeypatch, fake_converter):
    shelfd = load_shelfd(tmp_path, monkeypatch, fake_converter)
    with shelfd._git_lock:
        shelfd.ensure_repo()
    return shelfd


def cube_stl(size=10.0) -> bytes:
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
            (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    out = bytearray(b"\0" * 80 + struct.pack("<I", len(tris)))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0)
        for i in t:
            out += struct.pack("<3f", *v[i])
        out += b"\0\0"
    return bytes(out)


def zipped(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def save(shelfd, files, mid="42"):
    payload = {"source": "thingiverse", "id": mid, "title": "Test Bracket",
               "source_url": f"https://www.thingiverse.com/thing:{mid}",
               "uploads": [{"field": f"file{i}", "name": n} for i, n in enumerate(files)]}
    data = {"payload": json.dumps(payload)}
    for i, (name, blob) in enumerate(files.items()):
        data[f"file{i}"] = (io.BytesIO(blob), name)
    r = shelfd.app.test_client().post("/api/save", data=data, headers=AUTH,
                                      content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def convert(shelfd, files="all", mid="42"):
    return shelfd.app.test_client().post(
        "/api/convert", json={"source": "thingiverse", "id": mid, "files": files}, headers=AUTH)


def remote_model(tmp_path, mid="42"):
    """model.yaml and the list of committed files, as the remote has them."""
    remote = tmp_path / "remote.git"
    names = run("git", "ls-tree", "-r", "--name-only", "main", cwd=remote).split()
    d = next(n.rsplit("/", 1)[0] for n in names
             if n.startswith(f"models/thingiverse/{mid}-") and n.endswith("/model.yaml"))
    rec = yaml.safe_load(run("git", "show", f"main:{d}/model.yaml", cwd=remote))
    return rec, [n[len(d) + 1:] for n in names if n.startswith(d + "/")]


FILES = {
    "part.stl": cube_stl(),
    "thing-42.zip": zipped({"files/part.stl": cube_stl(5), "files/lid.3mf": b"3mf",
                            "README.txt": b"hi", "__MACOSX/files/._part.stl": b"x"}),
    "instructions.pdf": b"%PDF-1.4",
}


def test_save_lists_meshes_including_zip_contents(shelf):
    res = save(shelf, FILES)
    assert res["convert_available"] is True
    assert sorted(c["source"] for c in res["convertible"]) == [
        "files/part.stl",
        "files/thing-42.zip::files/lid.3mf",
        "files/thing-42.zip::files/part.stl",
    ]
    assert all(c["step"] is None for c in res["convertible"])


def test_convert_all(shelf, tmp_path):
    save(shelf, FILES)
    r = convert(shelf)
    assert r.status_code == 200, r.get_json()
    res = r.get_json()
    assert res["changed"] and all(x["ok"] for x in res["results"])

    rec, committed = remote_model(tmp_path)
    steps = {f["converted_from"]: f["path"] for f in rec["files"] if f.get("converted_from")}
    # two meshes are called part.stl: the second STEP gets a suffix, not the first's slot
    assert sorted(steps.values()) == ["files/step/lid.step", "files/step/part-2.step",
                                      "files/step/part.step"]
    assert set(steps.values()) <= set(committed)
    assert all(f["lfs"] for f in rec["files"] if f.get("converted_from"))

    # STEP files go to LFS like the meshes
    attrs = run("git", "check-attr", "filter", "--", "x/files/step/part.step",
                cwd=tmp_path / "work")
    assert attrs.strip().endswith("lfs")

    listing = shelf.app.test_client().get(
        "/api/convertible?source=thingiverse&id=42", headers=AUTH).get_json()
    assert all(c["step"] for c in listing["files"])


def test_convert_selected_only(shelf, tmp_path):
    save(shelf, FILES)
    res = convert(shelf, ["files/thing-42.zip::files/lid.3mf"]).get_json()
    assert [x["source"] for x in res["results"]] == ["files/thing-42.zip::files/lid.3mf"]
    rec, _ = remote_model(tmp_path)
    assert [f["path"] for f in rec["files"] if f.get("converted_from")] == ["files/step/lid.step"]


def test_reconvert_overwrites_same_file(shelf, tmp_path):
    save(shelf, FILES)
    convert(shelf, ["files/part.stl"])
    convert(shelf, ["files/part.stl"])
    rec, _ = remote_model(tmp_path)
    assert [f["path"] for f in rec["files"] if f.get("converted_from")] == ["files/step/part.step"]


def test_one_failure_does_not_stop_the_rest(shelf, tmp_path):
    save(shelf, {"good.stl": cube_stl(), "bad.stl": b"solid BROKEN"})
    res = convert(shelf).get_json()
    by = {x["source"]: x for x in res["results"]}
    assert by["files/good.stl"]["ok"] and by["files/good.stl"]["path"] == "files/step/good.step"
    assert not by["files/bad.stl"]["ok"] and "broken" in by["files/bad.stl"]["error"]
    rec, _ = remote_model(tmp_path)
    assert [f["converted_from"] for f in rec["files"] if f.get("converted_from")] == [
        "files/good.stl"]


def test_unknown_names_are_reported_not_converted(shelf):
    save(shelf, FILES)
    res = convert(shelf, ["files/instructions.pdf", "../../etc/passwd"]).get_json()
    assert not res["changed"]
    assert all(not x["ok"] for x in res["results"])


def test_bad_files_argument(shelf):
    save(shelf, FILES)
    assert convert(shelf, "files/part.stl").status_code == 400


def test_nothing_to_convert(shelf):
    save(shelf, {"instructions.pdf": b"%PDF-1.4"})
    assert convert(shelf).status_code == 400


def test_converts_from_lfs_pointers_in_a_fresh_clone(tmp_path, monkeypatch, fake_converter):
    """After a restart on a new box the work tree holds LFS pointers only, and
    older zips have no recorded contents: both have to be fetched."""
    shelfd = load_shelfd(tmp_path, monkeypatch, fake_converter)
    with shelfd._git_lock:
        shelfd.ensure_repo()
    save(shelfd, FILES)

    # forget zip contents, as a capture from before this feature would have
    work = tmp_path / "work"
    d = next((work / "models" / "thingiverse").iterdir())
    rec = yaml.safe_load((d / "model.yaml").read_text())
    for f in rec["files"]:
        f.pop("meshes", None)
    (d / "model.yaml").write_text(yaml.safe_dump(rec, sort_keys=False))
    run("git", "commit", "-qam", "old-style record", cwd=work)
    run("git", "push", "-q", "origin", "HEAD:main", cwd=work)

    shutil.rmtree(work)
    shelfd = load_shelfd(tmp_path, monkeypatch, fake_converter)
    with shelfd._git_lock:
        shelfd.ensure_repo()
    d = next((work / "models" / "thingiverse").iterdir())
    assert (d / "files" / "part.stl").read_bytes().startswith(shelfd.LFS_POINTER)

    res = convert(shelfd).get_json()
    assert len(res["results"]) == 3 and all(x["ok"] for x in res["results"]), res
    # the fake converter records the size it was given: real bytes, not a pointer
    rec, _ = remote_model(tmp_path)
    step = next(f for f in rec["files"] if f.get("converted_from") == "files/part.stl")
    oid = step["sha256"]
    obj = work / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid
    assert f"part.stl {len(cube_stl())} " in obj.read_text()


def test_disabled_without_converter(tmp_path, monkeypatch):
    shelfd = load_shelfd(tmp_path, monkeypatch, "")
    with shelfd._git_lock:
        shelfd.ensure_repo()
    c = shelfd.app.test_client()
    assert c.get("/api/health").get_json()["convert_available"] is False
    assert save(shelfd, FILES)["convert_available"] is False
    assert convert(shelfd).status_code == 501


@pytest.mark.skipif(not os.environ.get("SHELF_TEST_STL2STEP"),
                    reason="set SHELF_TEST_STL2STEP to a cad-transmogrifier stl2step launcher")
def test_real_converter(tmp_path, monkeypatch):
    shelfd = load_shelfd(tmp_path, monkeypatch, os.environ["SHELF_TEST_STL2STEP"])
    with shelfd._git_lock:
        shelfd.ensure_repo()
    save(shelfd, {"cube.stl": cube_stl(),
                  "thing-42.zip": zipped({"cube2.stl": cube_stl(20)})})
    res = convert(shelfd).get_json()
    assert all(x["ok"] for x in res["results"]), res
    d = next((tmp_path / "work" / "models" / "thingiverse").iterdir())
    for name in ("cube.step", "cube2.step"):
        text = (d / "files" / "step" / name).read_text()
        assert text.startswith("ISO-10303-21;") and "CLOSED_SHELL" in text
