"""Install the reviewed Linux/WSL preview without changing an existing workbench."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from email.parser import BytesParser
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from . import hermes as hermes_installer
from .launcher import LauncherError, hermes_runtime_files

VERSION = "0.4.0a2"
RUNTIME = "0.7.0a2"
MARKER = "INSTALLATION.json"
APPARMOR = b'''abi <abi/4.0>,
include <tunables/global>

profile yuanxingmu-bwrap /opt/yuanxingmu/bin/bwrap flags=(unconfined) {
  userns,
}
'''


class InstallError(RuntimeError):
    pass


def bundled(name: str) -> bytes:
    return files(__package__).joinpath(name).read_bytes()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(part)
    return value.hexdigest()


def record(path: Path) -> dict:
    return {"bytes": path.stat().st_size, "sha256": digest(path)}


def private(path: Path, *, directory=False):
    info = path.lstat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise InstallError("安装目录或记录的归属、访问权限不正确；已停止，未接管这个位置。")
    return info


def member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or "\\" in name or ":" in name or "\0" in name
            or any(p in {"", ".", ".."} for p in name.split("/"))):
        raise InstallError("安装包包含不安全的文件路径。")
    return path


def below(root: Path, name: str) -> Path:
    target = root.joinpath(*member(name).parts)
    for item in (target, *target.parents):
        if item == root:
            break
        if item.is_symlink():
            raise InstallError("安装位置出现符号链接；已停止，未写入链接目标。")
    if not target.resolve().is_relative_to(root.resolve()):
        raise InstallError("文件位置离开了本次安装目录。")
    return target


def atomic(path: Path, data: bytes, mode=0o600):
    if path.is_symlink():
        raise InstallError("不能覆盖符号链接。")
    fd, temporary = tempfile.mkstemp(prefix=".yxm-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(root: Path, state: dict):
    private(root, directory=True)
    atomic(root / MARKER, (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode())


def check_environment(root: Path, *, experimental_debian13=False, system_deps=False):
    if experimental_debian13 and system_deps:
        raise InstallError("Debian 13 实验验收只接受系统依赖已就绪的环境，不能使用 --system-deps。")
    if not sys.platform.startswith("linux") or platform.machine() != "x86_64":
        raise InstallError("安装器要求 Linux x86_64；默认支持 Ubuntu/WSL Ubuntu 24.04，Debian 13 仅供显式实验验收。")
    if sys.version_info < (3, 12) or not Path(sys.executable).resolve().is_relative_to("/usr"):
        raise InstallError("请用 Linux 系统的 /usr/bin/python3（3.12 或更新版本）运行安装器。")
    if os.geteuid() == 0:
        raise InstallError("请以自己的 Linux 用户运行安装器，不要在整条命令前加 sudo。")
    release = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            release[key] = value.strip('"')
    if experimental_debian13:
        if (release.get("ID"), release.get("VERSION_ID")) != ("debian", "13"):
            raise InstallError("--experimental-debian13 仅用于 Debian 13 的候选验收。")
        if (sys.version_info[:2] != (3, 13)
                or Path(sys.executable).resolve() != Path("/usr/bin/python3").resolve()):
            raise InstallError("Debian 13 实验验收须用 /usr/bin/python3 指向的系统 Python 3.13。")
        if not Path("/usr/bin/bwrap").is_file():
            raise InstallError("Debian 13 实验验收要求预先准备 /usr/bin/bwrap；本入口不会安装系统依赖。")
    elif (release.get("ID"), release.get("VERSION_ID")) != ("ubuntu", "24.04"):
        raise InstallError("当前发布版只验证了 Ubuntu 24.04；Debian 13 候选验收需显式 --experimental-debian13。")
    home = Path.home().absolute()
    if (".." in root.parts or root.resolve() != root or root == home
            or not root.is_relative_to(home) or root.is_relative_to("/mnt")):
        raise InstallError("请选 Linux 家目录下的新子目录，例如 ~/yuanxingmu。")
    for part in (root, *root.parents):
        if part.is_symlink():
            raise InstallError("安装目录及其父目录不能是符号链接。")
    if shutil.disk_usage(home).free < 2 * 1024 ** 3:
        raise InstallError("首次安装需要至少 2 GB 可用空间，请清理空间后重试。")


@contextmanager
def installation(root: Path, *, features=(), development=None, experimental_platform=None):
    import fcntl
    if experimental_platform not in (None, "debian13"):
        raise InstallError("未知的实验安装范围。")
    if not root.exists():
        # A failed mkdir or missing initial receipt never authorizes adoption.
        root.mkdir(mode=0o700)
        info = private(root, directory=True)
        state = {"schema_version": 1, "installer_version": VERSION, "runtime_version": RUNTIME,
                 "install_id": uuid.uuid4().hex, "root_identity": [info.st_dev, info.st_ino],
                 "status": "installing", "components": {}, "files": {}, "features": list(features)}
        if development is not None:
            state["development_wheel"] = development
        if experimental_platform is not None:
            state["experimental_platform"] = experimental_platform
        save(root, state)
    info = private(root, directory=True)
    # Unknown existing directories stay untouched, including no new lock file.
    # Only validate the receipt's type here; read its state after taking the lock.
    receipt = private(root / MARKER)
    if receipt.st_size > 16 * 1024 * 1024:
        raise InstallError("安装记录大小异常，已停止。")
    fd = os.open(root / ".install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private(root / ".install.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("同一目录已有安装在进行，请回到原来的窗口。") from None
        # Read after taking the lock: another invocation may have completed since
        # our initial directory observation. Never act on a pre-lock snapshot.
        locked_root = private(root, directory=True)
        if (locked_root.st_dev, locked_root.st_ino) != (info.st_dev, info.st_ino):
            raise InstallError("等待期间安装目录被替换，已停止。")
        lock_info = os.fstat(fd)
        current_lock = private(root / ".install.lock")
        if (lock_info.st_dev, lock_info.st_ino) != (current_lock.st_dev, current_lock.st_ino):
            raise InstallError("安装锁已被替换，已停止。")
        private(root / MARKER)
        state = json.loads((root / MARKER).read_text())
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or state.get("installer_version") != VERSION or state.get("runtime_version") != RUNTIME
                or state.get("root_identity") != [info.st_dev, info.st_ino]
                or not isinstance(state.get("install_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", state["install_id"])
                or state.get("status") not in {"installing", "complete"}
                or state.get("experimental_platform") != experimental_platform
                or not isinstance(state.get("components"), dict) or not isinstance(state.get("files"), dict)
                or state.get("features", []) not in ([], ["hermes"])
                or not set(state["components"]).issubset({"app", "node", "openclaw", "hermes"})
                or any(value != "complete" for value in state["components"].values())):
            raise InstallError("此目录不属于这版安装器；请保留原目录并选择一个新位置。")
        if state["status"] != "complete" and ((root / "workbench").exists() or (root / "workbench").is_symlink()):
            raise InstallError("未完成的安装目录中已有工作资料；为保留权限记录，已停止自动重试。")
        yield state
    finally:
        os.close(fd)


def command(root: Path, name: str, argv: list[str], *, env=None, timeout=900):
    logs = below(root, "install-logs")
    logs.mkdir(mode=0o700, exist_ok=True)
    private(logs, directory=True)
    log = logs / (name + "-" + uuid.uuid4().hex + ".log")
    fd = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        process = subprocess.Popen(argv, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Do not reap the group leader before killing the group: a descendant
            # can ignore TERM after its parent exits, and the leader PID must not
            # become reusable in between the two signals.
            try:
                time.sleep(.2)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            raise
    if code:
        raise InstallError(f"{name} 未完成（退出码 {code}）。记录在 {log}；可以重新运行同一安装器。")
    return log


def fetch(root: Path, spec: dict, cache: Path | None):
    downloads = below(root, "downloads")
    downloads.mkdir(mode=0o700, exist_ok=True)
    private(downloads, directory=True)
    name = member(spec["name"])
    if len(name.parts) != 1:
        raise InstallError("下载输入名称不正确。")
    target = below(root, "downloads/" + str(name))
    expected = {"bytes": spec["bytes"], "sha256": spec["sha256"]}
    if target.exists():
        private(target)
        if record(target) != expected:
            raise InstallError("已保存的下载文件校验不符，请保留目录并检查；不会执行这个文件。")
        return target
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=downloads)
    try:
        candidate = cache / spec["name"] if cache else None
        if candidate and candidate.is_file():
            source = candidate.open("rb")
        else:
            if not spec["url"].startswith("https://"):
                raise InstallError("下载地址必须使用 HTTPS。")
            request = urllib.request.Request(spec["url"], headers={"User-Agent": "Yuanxingmu-installer/" + VERSION})
            try:
                source = urllib.request.urlopen(request, timeout=45)
            except (urllib.error.URLError, OSError):
                raise InstallError("下载连接未完成。请检查网络后重新运行，已完成的组件会保留。") from None
        with source, os.fdopen(fd, "wb") as output:
            fd = None
            count = 0
            while part := source.read(1024 * 1024):
                count += len(part)
                if count > spec["bytes"]:
                    raise InstallError("下载大小超出固定版本记录，已停止。")
                output.write(part)
            output.flush()
            os.fsync(output.fileno())
        if record(Path(temporary)) != expected:
            raise InstallError("下载校验未通过，已停止；不会解压或执行这个文件。")
        os.replace(temporary, target)
        return target
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)


def clear_partial(root: Path, name: str):
    target = below(root, name)
    if target.exists():
        private(target, directory=True)
        shutil.rmtree(target)


def development_wheel(path: Path | None, sha256: str | None):
    """Validate a deliberate local preview input before touching an install root."""
    if path is None and sha256 is None:
        return None
    if path is None or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise InstallError("开发验收需要同时填写 --development-wheel 和它的完整小写 SHA256。")
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 20 * 1024 ** 2:
        raise InstallError("开发 wheel 必须是实际的普通文件，且不超过 20 MiB。")
    details = record(path)
    if details["sha256"] != sha256:
        raise InstallError("开发 wheel 的 SHA256 与传入值不一致，未安装。")
    with zipfile.ZipFile(path) as wheel:
        metadata_name = "agent_defense_check-" + RUNTIME + ".dist-info/METADATA"
        metadata = [item for item in wheel.infolist() if item.filename.endswith(".dist-info/METADATA")]
        if (len(metadata) != 1 or metadata[0].filename != metadata_name
                or metadata[0].file_size > 1024 ** 2):
            raise InstallError("开发 wheel 的发行名称或版本与当前安装器不符。")
        message = BytesParser().parsebytes(wheel.read(metadata[0]))
        if message.get("Name") != "agent-defense-check" or message.get("Version") != RUNTIME:
            raise InstallError("开发 wheel 必须与当前安装器的程序版本 " + RUNTIME + " 一致。")
        try:
            package_version = re.search(r'^__version__\s*=\s*[\'"]([^\'"]+)',
                                       wheel.read("yuanxingmu/__init__.py").decode("utf-8"), re.M)
        except KeyError:
            package_version = None
        if package_version is None or package_version.group(1) != RUNTIME:
            raise InstallError("开发 wheel 的 Python 程序版本与发行元数据不一致。")
        if "yuanxingmu/hermes.py" not in wheel.namelist():
            raise InstallError("这个开发 wheel 尚未包含 Hermes 接入，请先构建当前源码。")
    return {"name": path.name, **details, "version": RUNTIME, "url": "development:local"}


def extract_wheel(root: Path, archive: Path):
    app = below(root, "app")
    app.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as wheel:
        names = set()
        if sum(item.file_size for item in wheel.infolist()) > 20 * 1024 ** 2:
            raise InstallError("Python 包解压大小异常。")
        for item in wheel.infolist():
            if item.is_dir():
                continue
            path = member(item.filename)
            if (item.filename in names or path.parts[0] not in {"yuanxingmu", "defensecheck", "agent_defense_check-" + RUNTIME + ".dist-info"}
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise InstallError("Python 包内容与安装范围不符。")
            names.add(item.filename)
            target = below(app, item.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(wheel.read(item))
            target.chmod(0o600)
    return {"app/" + name: record(app / name) for name in sorted(names)}


def extract_node(root: Path, archive: Path):
    destination = below(root, "tools")
    destination.mkdir(mode=0o700)
    with tarfile.open(archive, "r:xz") as node:
        entries = node.getmembers()
        if sum(item.size for item in entries) > 400 * 1024 ** 2:
            raise InstallError("Node 解压大小异常。")
        for item in entries:
            path = member(item.name.rstrip("/"))
            if path.parts[0] != "node-v24.16.0-linux-x64" or item.isdev() or item.isfifo() or item.islnk():
                raise InstallError("Node 包含不支持的文件。")
            if item.issym():
                # npm/npx/corepack are links within the reviewed Node tree.
                target = PurePosixPath(item.linkname)
                actual = (destination / path.parent / str(target)).resolve()
                if target.is_absolute() or not actual.is_relative_to(destination / "node-v24.16.0-linux-x64"):
                    raise InstallError("Node 链接目标离开安装目录。")
        node.extractall(destination, members=entries, filter="data")
    return {"tools/node-v24.16.0-linux-x64/bin/node": record(destination / "node-v24.16.0-linux-x64/bin/node")}


def verify_node_tree(root: Path, archive: Path):
    """Check npm's whole tree against the hash-verified archive before execution."""
    destination = below(root, "tools")
    expected_names = set()
    with tarfile.open(archive, "r:xz") as node:
        for item in node:
            name = member(item.name.rstrip("/")).as_posix()
            if name in expected_names or PurePosixPath(name).parts[0] != "node-v24.16.0-linux-x64":
                raise InstallError("Node 安装包的文件记录异常。")
            expected_names.add(name)
            path = destination / name
            # Parents may never be links; a leaf can only be the exact archive link.
            if "/" in name:
                below(destination, str(PurePosixPath(name).parent))
            if item.issym():
                if not path.is_symlink() or os.readlink(path) != item.linkname:
                    raise InstallError("Node 的安装链接发生变化；不会运行已有 npm。")
            elif item.isdir():
                if path.is_symlink() or not path.is_dir():
                    raise InstallError("Node 的安装目录发生变化。")
            elif item.isfile():
                if path.is_symlink() or not path.is_file():
                    raise InstallError("Node 的安装文件类型发生变化。")
                with node.extractfile(item) as source:
                    expected = {"bytes": item.size, "sha256": hashlib.file_digest(source, "sha256").hexdigest()}
                if record(path) != expected:
                    raise InstallError("Node 或 npm 文件发生变化；不会运行这个安装环境。")
            else:
                raise InstallError("Node 安装包包含不支持的文件。")
    actual_names = {p.relative_to(destination).as_posix() for p in destination.rglob("*")}
    if actual_names != expected_names:
        raise InstallError("Node 安装目录中的文件清单发生变化；不会运行已有 npm。")


def app_python(root: Path, code: str, *args: str):
    bootstrap = "import sys;sys.path.insert(0,sys.argv.pop(1));" + code
    return ["/usr/bin/python3", "-I", "-B", "-c", bootstrap, str(root / "app"), *args]


def system_dependencies(root: Path):
    restricted = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    needs_apparmor = (restricted.exists() and restricted.read_text().strip() == "1"
                      and not Path("/usr/sbin/apparmor_parser").is_file())
    if not Path("/usr/bin/bwrap").is_file() or needs_apparmor:
        print("需要安装 Ubuntu 的隔离组件；sudo 可能询问一次你的 Ubuntu 密码。", flush=True)
        if subprocess.run(["/usr/bin/sudo", "-v"]).returncode:
            raise InstallError("没有完成系统组件授权，安装已停止。")
        command(root, "更新组件列表", ["/usr/bin/sudo", "-n", "/usr/bin/apt-get", "update"])
        command(root, "安装隔离组件", ["/usr/bin/sudo", "-n", "/usr/bin/apt-get", "install", "-y", "bubblewrap", "ca-certificates", "apparmor"])


def scoped_apparmor(root: Path):
    if subprocess.run(["/usr/bin/sudo", "-v"]).returncode:
        raise InstallError("没有完成系统组件授权，隔离配置未改变。")
    target = Path("/opt/yuanxingmu/bin/bwrap")
    for part in (target, target.parent, target.parent.parent, Path("/opt")):
        if part.is_symlink() or (part.exists() and (part.stat().st_uid != 0 or part.stat().st_mode & 0o022)):
            raise InstallError("系统隔离组件目录的归属或权限异常。")
    if target.exists():
        if digest(target) != digest(Path("/usr/bin/bwrap")):
            raise InstallError("系统中已有另一份元星木隔离组件，保留原文件；请参考排错指南。")
    else:
        command(root, "准备隔离组件目录", ["/usr/bin/sudo", "-n", "/usr/bin/install", "-d", "-m", "0755", str(target.parent)])
        command(root, "固定隔离组件", ["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "0755", "/usr/bin/bwrap", str(target)])
    profile = Path("/etc/apparmor.d/yuanxingmu-bwrap")
    for part in (Path("/etc"), profile.parent):
        if part.is_symlink() or (part.exists() and (not part.is_dir() or part.stat().st_uid != 0 or part.stat().st_mode & 0o022)):
            raise InstallError("系统隔离配置目录的归属或权限异常。")
    if profile.is_symlink():
        raise InstallError("已有同名 AppArmor 链接；不会覆盖，请参考排错指南。")
    if profile.exists():
        if not profile.is_file() or profile.stat().st_uid != 0 or profile.stat().st_mode & 0o022 or profile.read_bytes() != APPARMOR:
            raise InstallError("已有同名 AppArmor 配置；不会覆盖，请参考排错指南。")
    else:
        source = root / "apparmor-profile.txt"
        atomic(source, APPARMOR)
        command(root, "保存隔离配置", ["/usr/bin/sudo", "-n", "/usr/bin/install", "-m", "0644", str(source), str(profile)])
    command(root, "启用隔离配置", ["/usr/bin/sudo", "-n", "/usr/sbin/apparmor_parser", "--replace", str(profile)])
    return target


def isolation(root: Path, system_deps: bool, *, experimental_debian13=False):
    if experimental_debian13 and system_deps:
        raise InstallError("Debian 13 实验验收不能自动安装系统依赖或配置 AppArmor。")
    if system_deps:
        system_dependencies(root)
    bwrap = Path("/usr/bin/bwrap")
    if not bwrap.is_file():
        if experimental_debian13:
            raise InstallError("Debian 13 实验验收缺少预先准备的 bwrap；不会尝试 sudo 或系统策略修改。")
        raise InstallError("缺少 Ubuntu 隔离组件。请重新运行此安装器，加上 --system-deps。")
    code = "from yuanxingmu.cli import main;raise SystemExit(main())"
    try:
        command(root, "检查实际隔离", app_python(root, code, "doctor", "--bwrap", str(bwrap)), timeout=30)
    except InstallError:
        restricted = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
        if not (system_deps and restricted.exists() and restricted.read_text().strip() == "1"):
            raise
        print("正在为指定隔离组件配置 Ubuntu 权限；保留系统的全局限制。", flush=True)
        bwrap = scoped_apparmor(root)
        command(root, "复查实际隔离", app_python(root, code, "doctor", "--bwrap", str(bwrap)), timeout=30)
    return bwrap.resolve()


def npm_install(root: Path, pins: dict):
    npm = root / "tools/node-v24.16.0-linux-x64/bin/npm"
    package = bundled("openclaw-package.json")
    lock = bundled("openclaw-package-lock.json")
    if (hashlib.sha256(package).hexdigest() != pins["openclaw"]["package_sha256"]
            or hashlib.sha256(lock).hexdigest() != pins["openclaw"]["lock_sha256"]):
        raise InstallError("安装器的 OpenClaw 依赖记录不匹配。")
    target = below(root, "openclaw")
    target.mkdir(mode=0o700, exist_ok=True)
    private(target, directory=True)
    atomic(target / "package.json", package)
    atomic(target / "package-lock.json", lock)
    for name in ("npm-home", "npm-cache", "tmp"):
        path = below(root, name)
        path.mkdir(mode=0o700, exist_ok=True)
        private(path, directory=True)
    user_config = below(root, "npm-user.conf")
    global_config = below(root, "npm-global.conf")
    atomic(user_config, b"")
    atomic(global_config, b"")
    env = {"HOME": str(root / "npm-home"), "PATH": str(npm.parent) + ":/usr/bin:/bin", "LANG": "C.UTF-8",
           "TMPDIR": str(root / "tmp"), "npm_config_userconfig": str(user_config),
           "npm_config_globalconfig": str(global_config), "npm_config_cache": str(root / "npm-cache"),
           "npm_config_registry": "https://registry.npmjs.org/", "npm_config_update_notifier": "false"}
    # A user's network proxy may be needed to download public packages. Do not log it.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if key in os.environ:
            env[key] = os.environ[key]
    command(root, "安装OpenClaw", [str(npm), "ci", "--prefix", str(target), "--omit=dev", "--no-audit", "--no-fund"], env=env)
    if (target / "node_modules/openclaw/.openclaw-lifecycle-pending").exists():
        raise InstallError("OpenClaw 安装后步骤尚未完成。")
    if (target / "package-lock.json").read_bytes() != lock:
        raise InstallError("安装后的依赖锁文件发生变化。")
    return {"openclaw/node_modules/openclaw/" + name: record(target / "node_modules/openclaw" / name)
            for name in ("package.json", "openclaw.mjs")}


def verify_files(root: Path, state: dict):
    components = state.get("components")
    inventory = state.get("files")
    if (not isinstance(components, dict) or not isinstance(inventory, dict)
            or set(components) not in (set(), {"app"}, {"app", "node"}, {"app", "node", "openclaw"}, {"app", "node", "openclaw", "hermes"})
            or any(value != "complete" for value in components.values())):
        raise InstallError("安装组件记录不完整。")
    features = state.get("features", [])
    if features not in ([], ["hermes"]) or ("hermes" in components and "hermes" not in features):
        raise InstallError("Hermes 组件与安装功能记录不一致。")
    required = set()
    if "app" in components:
        required |= {"app/yuanxingmu/" + name for name in (
            "__init__.py", "cli.py", "dashboard/__init__.py", "dashboard/server.py", "dashboard/web/index.html",
            "dashboard/web/app.js", "dashboard/web/styles.css", "dashboard/web/mark.svg")}
        app = below(root, "app")
        if not app.is_dir():
            raise InstallError("已安装的程序目录缺失。")
        required |= {p.relative_to(root).as_posix() for p in app.rglob("*") if p.is_file() or p.is_symlink()}
    if "node" in components:
        required.add("tools/node-v24.16.0-linux-x64/bin/node")
    if "openclaw" in components:
        required |= {"openclaw/node_modules/openclaw/" + name for name in ("package.json", "openclaw.mjs")}
    if "hermes" in components:
        try:
            required |= hermes_runtime_files(root) | {"hermes/SOURCE.json", "hermes/build-constraints.txt", "hermes/uv.toml"}
        except (LauncherError, OSError) as exc:
            raise InstallError("Hermes 运行文件缺失或路径发生变化。") from exc
    if state.get("status") == "complete":
        paths = state.get("paths", {})
        if (not isinstance(paths, dict) or paths.get("app") != "app" or paths.get("node") != "tools/node-v24.16.0-linux-x64/bin/node"
                or paths.get("openclaw") != "openclaw/node_modules/openclaw"
                or paths.get("bwrap") not in {"/usr/bin/bwrap", "/opt/yuanxingmu/bin/bwrap"}
                or set(state["components"]) != {"app", "node", "openclaw", *features}
                or "hermes" in features and (paths.get("hermes_python") != "hermes/env/bin/python"
                                              or paths.get("hermes_source") != "hermes/source")):
            raise InstallError("已完成安装的运行路径或组件记录不完整。")
        required |= {"open-yuanxingmu", "bwrap:" + paths["bwrap"]}
        if record(root / "open-yuanxingmu")["sha256"] != state.get("launcher_sha256"):
            raise InstallError("日常启动入口与安装记录不符。")
    if not required.issubset(inventory):
        raise InstallError("安装校验记录没有覆盖已完成组件的全部必需文件。")
    for name, expected in inventory.items():
        if (not isinstance(name, str) or not isinstance(expected, dict) or set(expected) != {"bytes", "sha256"}
                or type(expected["bytes"]) is not int or expected["bytes"] < 0
                or not isinstance(expected["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"])):
            raise InstallError("运行文件的校验记录无效。")
        if name.startswith("bwrap:") and name not in {"bwrap:/usr/bin/bwrap", "bwrap:/opt/yuanxingmu/bin/bwrap"}:
            raise InstallError("隔离组件路径不属于这版安装器。")
        path = Path(name.removeprefix("bwrap:")) if name.startswith("bwrap:") else below(root, name)
        if not path.is_file() or path.is_symlink() or record(path) != expected:
            raise InstallError("已安装的运行文件发生变化；不会覆盖或修复已有工作的运行环境。")


def verify_runtime(root: Path, bwrap: Path, *, hermes=False):
    code = ("from pathlib import Path;from yuanxingmu.dashboard.server import Runtime;"
            "r=Runtime(Path(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3])).public();"
            "print(r);raise SystemExit(0 if r['available'] else 2)")
    command(root, "核对完整运行环境", app_python(root, code, str(root / "tools/node-v24.16.0-linux-x64/bin/node"),
        str(root / "openclaw/node_modules/openclaw"), str(bwrap)), timeout=40)
    if hermes:
        code = ("from pathlib import Path;from yuanxingmu.dashboard.server import Runtime;"
                "r=Runtime.discover(Path(sys.argv[1]),bwrap=sys.argv[2]).hermes_public();"
                "print(r);raise SystemExit(0 if r['available'] else 2)")
        command(root, "核对Hermes运行环境", app_python(root, code, str(root), str(bwrap)), timeout=40)


def require_hermes_app(root: Path, pins: dict):
    code = ("from yuanxingmu.hermes import HERMES_VERSION,HERMES_COMMIT;"
            "from yuanxingmu.dashboard.server import Runtime;"
            "assert hasattr(Runtime,'hermes_public');"
            "assert HERMES_VERSION==sys.argv[1] and HERMES_COMMIT==sys.argv[2]")
    command(root, "核对程序中的Hermes接入", app_python(root, code, pins["hermes"]["version"], pins["hermes"]["commit"]), timeout=30)


def desktop_entry(root: Path):
    target = Path.home() / ".local/share/applications/yuanxingmu-workbench.desktop"
    if target.exists() or target.is_symlink():
        print("已有同名应用入口，已保留；可使用本次目录里的 open-yuanxingmu。", flush=True)
        return
    if any(part.is_symlink() for part in (target.parent, *target.parent.parents)):
        print("应用目录使用了链接，未写入快捷方式。", flush=True)
        return
    # Desktop Exec is not a shell. Escape its quoted argument according to the spec.
    value = str(root / "open-yuanxingmu")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    contents = ("[Desktop Entry]\nType=Application\nName=元星木\nComment=资料与权限由你决定\n"
                f'Exec=/usr/bin/python3 -I "{escaped}"\nTerminal=true\nCategories=Utility;\n').encode()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves another install's shortcut even during a race.
    with target.open("xb") as stream:
        stream.write(contents)
    target.chmod(0o600)


def install(root: Path, *, system_deps=False, cache=None, shortcut=True, dev_wheel=None, dev_sha256=None,
            experimental_debian13=False):
    check_environment(root, experimental_debian13=experimental_debian13, system_deps=system_deps)
    pins = json.loads(bundled("pins.json"))
    if pins["installer_version"] != VERSION or pins["runtime_version"] != RUNTIME:
        raise InstallError("安装器与固定版本记录不一致。")
    local_wheel = development_wheel(dev_wheel, dev_sha256)
    features = ["hermes"] if local_wheel else pins.get("runtime_features", [])
    if features not in ([], ["hermes"]):
        raise InstallError("安装器的运行功能记录不受支持。")
    with installation(root, features=features, development=local_wheel,
                      experimental_platform="debian13" if experimental_debian13 else None) as state:
        if experimental_debian13:
            print("这是 Debian 13 实验验收候选，不代表已发布的发行版支持；不会修改系统隔离策略。", flush=True)
        if local_wheel is not None and (state["status"] == "complete" or state.get("development_wheel") != local_wheel):
            raise InstallError("开发 wheel 只能用于新目录，或使用同一 wheel 恢复未完成的开发安装；不会更换已有运行程序。")
        if state.get("development_wheel") and state["status"] != "complete" and local_wheel is None:
            raise InstallError("请带上原来的开发 wheel 和 SHA256 继续这次安装。")
        use_hermes = "hermes" in state.get("features", [])
        verify_files(root, state)
        if state["status"] == "complete":
            if use_hermes:
                verify_runtime(root, Path(state["paths"]["bwrap"]), hermes=True)
            else:
                verify_runtime(root, Path(state["paths"]["bwrap"]))
            print("这个目录已经装好，运行文件检查通过。没有覆盖已有资料或运行版本。", flush=True)
            return state
        components = state["components"]
        steps = "5" if use_hermes else "4"
        print("1/" + steps + " 准备元星木（固定版本 " + RUNTIME + "）", flush=True)
        if local_wheel:
            print("这是本地开发 wheel 验收，不是已发布的正式安装。", flush=True)
        if components.get("app") != "complete":
            wheel = fetch(root, local_wheel, dev_wheel.expanduser().absolute().parent) if local_wheel else fetch(root, pins["wheel"], cache)
            clear_partial(root, "app")
            state["files"].update(extract_wheel(root, wheel))
            components["app"] = "complete"
            save(root, state)
        verify_files(root, state)
        if use_hermes:
            require_hermes_app(root, pins)
        print("2/" + steps + " 检查这台电脑的实际隔离能力", flush=True)
        bwrap = (isolation(root, False, experimental_debian13=True) if experimental_debian13
                 else isolation(root, system_deps))
        state["files"]["bwrap:" + str(bwrap)] = record(bwrap)
        save(root, state)
        print("3/" + steps + " 安装固定版本的 Node.js 与 OpenClaw（首次可能需要几分钟）", flush=True)
        archive = fetch(root, pins["node"], cache)
        if components.get("node") != "complete":
            clear_partial(root, "tools")
            state["files"].update(extract_node(root, archive))
            components["node"] = "complete"
            save(root, state)
        verify_node_tree(root, archive)
        if components.get("openclaw") != "complete":
            # Only an owned incomplete install with no workbench reaches this branch.
            clear_partial(root, "openclaw")
            state["files"].update(npm_install(root, pins))
            components["openclaw"] = "complete"
            save(root, state)
        if use_hermes:
            print("4/5 安装 Hermes 独立环境，构建官方网页与终端（首次可能需要几分钟）", flush=True)
            if components.get("hermes") != "complete":
                state["files"].update(hermes_installer.install(root, pins, cache))
                components["hermes"] = "complete"
                save(root, state)
        print(steps + "/" + steps + " 核对运行环境并准备日常入口", flush=True)
        verify_files(root, state)
        if use_hermes:
            verify_runtime(root, bwrap, hermes=True)
        else:
            verify_runtime(root, bwrap)
        launcher = bundled("launcher.py")
        if not launcher.startswith(b"#!/usr/bin/python3"):
            raise InstallError("日常启动器缺少固定解释器声明。")
        atomic(root / "open-yuanxingmu", launcher, 0o700)
        state["launcher_sha256"] = hashlib.sha256(launcher).hexdigest()
        state["files"]["open-yuanxingmu"] = record(root / "open-yuanxingmu")
        state["paths"] = {"app": "app", "node": "tools/node-v24.16.0-linux-x64/bin/node",
                          "openclaw": "openclaw/node_modules/openclaw", "bwrap": str(bwrap)}
        if use_hermes:
            state["paths"].update({"hermes_python": "hermes/env/bin/python", "hermes_source": "hermes/source"})
        state["status"] = "complete"
        save(root, state)
        if shortcut:
            desktop_entry(root)
        return state


def main(argv=None):
    parser = argparse.ArgumentParser(description="元星木安装器：Ubuntu / WSL Ubuntu 24.04 x86_64 预览版")
    parser.add_argument("--install-root", type=Path, default=Path.home() / "yuanxingmu", help="Linux 家目录下的新子目录")
    parser.add_argument("--system-deps", action="store_true", help="允许通过 sudo 安装隔离组件、配置指定组件的 Ubuntu 权限")
    parser.add_argument("--experimental-debian13", action="store_true", help="仅供 Debian 13 / 系统 Python 3.13 实机验收；要求依赖预置，不能与 --system-deps 同用")
    parser.add_argument("--download-cache", type=Path, help="可复用下载目录；所有文件仍需核对固定 SHA256")
    parser.add_argument("--no-shortcut", action="store_true", help="不创建 Linux 应用入口")
    parser.add_argument("--development-wheel", type=Path, help="仅本机开发验收：使用当前版本的本地 wheel，并安装 Hermes")
    parser.add_argument("--development-wheel-sha256", help="本地开发 wheel 的完整 SHA256；必须与 --development-wheel 一起提供")
    args = parser.parse_args(argv)
    os.umask(0o077)
    root = args.install_root.expanduser().absolute()
    try:
        result = install(root, system_deps=args.system_deps, cache=args.download_cache, shortcut=not args.no_shortcut,
                         dev_wheel=args.development_wheel, dev_sha256=args.development_wheel_sha256,
                         experimental_debian13=args.experimental_debian13)
        print(f"\n安装完成。打开工作台：\n{root / 'open-yuanxingmu'}\n", flush=True)
        frameworks = "OpenClaw 或 Hermes" if "hermes" in result.get("features", []) else "OpenClaw"
        print("进入后可以连接自己的模型、选择资料并打开 " + frameworks + "。请保留工作台终端；首次模型连接仍需自行填写。", flush=True)
        return 0
    except (InstallError, OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile, tarfile.TarError) as exc:
        print("安装没有完成：" + str(exc), file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        print("安装已中断。保留安装目录，下次运行同一安装器可以继续。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
