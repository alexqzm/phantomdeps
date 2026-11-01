#!/usr/bin/env python3
"""Write the review archive into a git work tree, so that the repository holds what the archive
holds.

The archive (`make anon-artifact`, checked by tools/check_anon.py) is the only source: every file of
the archive is written to DEST with its permissions, files of DEST that the archive no longer holds
are deleted, and DEST/.git is left alone.  One change of form: git cannot track a directory that
holds its own .git, so each clone data/snapshots/git/<name>/ of the archive becomes the file
data/snapshots/git/<name>.tar (`make git-snapshots` unpacks it; clones already unpacked in DEST are
kept).  The tar files are deterministic (sorted members, owner 0, the archive's UTC times), so an
unchanged clone gives an identical file and no new commit.

Usage: python3 tools/export_repo.py ARCHIVE DEST     (authors' side: `make repo-export REPO=DEST`)
"""
from __future__ import annotations

import io
import os
import re
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

GIT_SNAP = "data/snapshots/git/"
CLONE = re.compile(r"^(data/snapshots/git/[^/]+)/\.git/")


def utc_seconds(info: zipfile.ZipInfo) -> int:
    """Entry time as written by `TZ=UTC zip`."""
    return int(datetime(*info.date_time, tzinfo=timezone.utc).timestamp())


def mode_of(info: zipfile.ZipInfo, default: int) -> int:
    return ((info.external_attr >> 16) & 0o777) or default


def safe(name: str) -> bool:
    p = PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts and p.parts[:1] != (".git",)


def clone_tar(z: zipfile.ZipFile, members: list[zipfile.ZipInfo]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as t:
        for i in sorted(members, key=lambda i: i.filename):
            ti = tarfile.TarInfo(i.filename[len(GIT_SNAP):].rstrip("/"))
            ti.mtime = utc_seconds(i)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if i.is_dir():
                ti.type, ti.mode = tarfile.DIRTYPE, mode_of(i, 0o755)
                t.addfile(ti)
            else:
                data = z.read(i)
                ti.size, ti.mode = len(data), mode_of(i, 0o644)
                t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def write(path: Path, data: bytes, mode: int) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    changed = not path.is_file() or path.read_bytes() != data
    if changed:
        path.write_bytes(data)
    os.chmod(path, 0o755 if mode & 0o111 else 0o644)
    return changed


def main(archive: str, dest: str) -> int:
    out = Path(dest)
    if not (out / ".git").is_dir():
        print(f"{dest}: not a git work tree (git init it first)")
        return 2
    z = zipfile.ZipFile(archive)
    infos = z.infolist()
    bad = [i.filename for i in infos if not safe(i.filename)]
    if bad:
        print(f"refusing unsafe archive paths: {bad[:5]}")
        return 2
    clones = sorted({m.group(1) for i in infos if (m := CLONE.match(i.filename))})
    files: dict[str, tuple[bytes, int]] = {}
    for i in infos:
        if i.is_dir() or any(i.filename.startswith(c + "/") for c in clones):
            continue
        files[i.filename] = (z.read(i), mode_of(i, 0o644))
    for c in clones:
        members = [i for i in infos if i.filename.startswith(c + "/")]
        files[c + ".tar"] = (clone_tar(z, members), 0o644)

    changed = sum(write(out / name, data, mode) for name, (data, mode) in sorted(files.items()))
    unpacked = {c + "/" for c in clones}   # clones that make git-snapshots unpacked in DEST stay
    removed = 0
    for p in sorted(out.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = p.relative_to(out).as_posix()
        if rel == ".git" or rel.startswith(".git/") or any((rel + "/").startswith(u)
                                                             for u in unpacked):
            continue
        if p.is_symlink() or p.is_file():
            if rel not in files:
                p.unlink()
                removed += 1
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    print(f"{dest}: {len(files)} files ({len(clones)} git clones as .tar), {changed} written, "
          f"{removed} removed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
