"""Check that rerunning the installer preserves runtime bindings and saved work."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


def snapshot(root):
    marker = root / "INSTALLATION.json"
    state = json.loads(marker.read_text())
    if state["status"] != "complete" or not (root / "workbench/catalog.json").is_file():
        raise RuntimeError("Reuse acceptance requires a completed installation with saved work")
    paths = {marker, root / "workbench", *(root / "workbench").rglob("*")}
    paths.update(Path(name[6:]) if name.startswith("bwrap:") else root / name for name in state["files"])
    result = {}
    for path in paths:
        info = path.lstat()
        identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
        if stat.S_ISLNK(info.st_mode):
            content = ("link", os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            with path.open("rb") as source:
                content = ("file", hashlib.file_digest(source, "sha256").hexdigest())
        elif stat.S_ISDIR(info.st_mode):
            content = ("directory",)
        else:
            content = ("other",)
        result[path] = identity + content
    return result


def installer_command(installer, root, *, experimental_debian13=False):
    command = ["/usr/bin/python3", "-I", str(installer), "--install-root", str(root), "--no-shortcut"]
    if experimental_debian13:
        command.append("--experimental-debian13")
    return command


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--experimental-debian13", action="store_true", help="将 Debian 13 实验验收开关传给同一候选安装器")
    args = parser.parse_args()
    before = snapshot(args.install_root)
    subprocess.run(installer_command(args.installer, args.install_root, experimental_debian13=args.experimental_debian13),
                   check=True, timeout=120)
    unchanged = snapshot(args.install_root) == before
    print(json.dumps({"passed": unchanged, "saved_work_and_bound_runtime_unchanged": unchanged}))
    raise SystemExit(0 if unchanged else 1)
