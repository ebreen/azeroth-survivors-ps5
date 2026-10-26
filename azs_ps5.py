#!/usr/bin/env python3
"""Put Azeroth Survivors (the Windows build) on a jailbroken PS5 running prospero-win.

    azs_ps5.py fetch [DIR]                     download the Windows build (.rar) from the
                                               developers' mirror, checking its SHA-256
    azs_ps5.py install SOURCE --host PS5_IP    copy the game, its profile and its
                                               controller preset to the console
    azs_ps5.py update --host PS5_IP            bring the console's copy to the latest
                                               official version (exe and game data)
    azs_ps5.py status --host PS5_IP            what is installed, and whether it is current
    azs_ps5.py uninstall --host PS5_IP         remove the game and its profile

SOURCE is the AzerothSurvivorsWindowsNew.rar from https://wow.aethus.pl/, a .zip of it,
or a folder that holds AzerothSurvivors.exe and its Data folder.

Instead of --host, --to DIR writes the same layout into a local folder (it mirrors
/data/prospero-win), for copying with your own FTP client or a USB drive.

Only Python's standard library is needed. Extracting a .rar needs one of: bsdtar
(Windows 10+'s and macOS's built-in `tar`), unrar, 7z/7zz or unar.
"""
from __future__ import annotations

import argparse
import ftplib
import gzip
import hashlib
import html
import io
import json
import os
import posixpath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PS5_FILES = HERE / "ps5"

SLUG = "azeroth-survivors"
EXE = "AzerothSurvivors.exe"
PREFIX_DIR = f"prefixes/{SLUG}"                           # the game's own Wine prefix on the console
GAME_DIR = f"{PREFIX_DIR}/drive_c/Games/AzerothSurvivors" # its C:\Games\AzerothSurvivors
LEGACY_GAME_DIR = "prefix/drive_c/Games/AzerothSurvivors" # where 0.1 put it (the default prefix; it can't run there)
PROFILE = f"profiles/{SLUG}.profile"
PROFILE_LIST = "profiles/profiles.lst"
PRESET = f"input/{SLUG}.input"
MARKER = f"{GAME_DIR}/azs-ps5.json"                      # what this tool last put there
CATALOG_MAX = 16                                          # the launcher drops profiles.lst above this

# The prefix: built by tools/build_prefix.sh (GitHub Actions) from prospero-win's
# release and published as a release of this repository; ps5/prefix.json pins it.
PREFIX_MANIFEST = PS5_FILES / "prefix.json"
PREFIX_URL = os.environ.get("AZS_PREFIX_URL", "")         # overrides the manifest's url (tests, mirrors)
REGISTRY = ("system.reg", "user.reg", "userdef.reg")      # sent last: their presence marks a whole prefix
# Wine reads HKCU\Environment into the game's environment. wgpu hides the console's
# RADV adapter as "not Vulkan compliant" without the first ("Unable to find a GPU!").
GAME_ENV = {"WGPU_ALLOW_UNDERLYING_NONCOMPLIANT_ADAPTER": "1", "WGPU_BACKEND": "vulkan"}

# Where `fetch` gets the Windows build: the developers' fast download mirror (its page
# names the current .rar, its version and its SHA-256), falling back to the official
# link on wow.aethus.pl, which is much slower.
MIRROR_PAGE = os.environ.get("AZS_MIRROR_URL", "https://azerothsurvivors-mirror.sandervspl.dev/")
DOWNLOAD_URL = os.environ.get("AZS_DOWNLOAD_URL", "https://wow.aethus.pl/download/AzerothSurvivorsWindowsNew.rar")
# The game's own update channel (the same URLs the game's updater reads, overridable
# the same way: WOW_ROGUE_UPDATE_URL / WOW_ROGUE_DATA_URL).
UPDATE_URL = os.environ.get("WOW_ROGUE_UPDATE_URL", "https://wow.aethus.pl/exe/latest.json")
DATA_URL = os.environ.get("WOW_ROGUE_DATA_URL", "https://wow.aethus.pl/exe/data/patches.json")
USER_AGENT = "azeroth-survivors-ps5 (+https://github.com/ebreen/azeroth-survivors-ps5)"

CHUNK = 4 << 20
CACHE = Path(os.environ.get("AZS_PS5_CACHE", Path.home() / ".cache" / "azeroth-survivors-ps5"))


class Failure(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class Progress:
    """One status line per file, redrawn at most a few times a second."""

    def __init__(self, label: str, total: int):
        self.label, self.total, self.done = label, total, 0
        self.start = self.last = time.monotonic()
        self.tty = sys.stdout.isatty()

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if self.tty and now - self.last > 0.25:
            self.last = now
            self.draw(now)

    def draw(self, now: float) -> None:
        rate = self.done / max(now - self.start, 1e-6)
        pct = f"{100 * self.done / self.total:3.0f}%" if self.total else ""
        sys.stdout.write(f"\r  {self.label}: {pct} {human(self.done)} of {human(self.total)}  {human(rate)}/s   ")
        sys.stdout.flush()

    def finish(self) -> None:
        now = time.monotonic()
        if self.tty:
            sys.stdout.write("\r" + " " * 100 + "\r")
        rate = self.done / max(now - self.start, 1e-6)
        log(f"  {self.label}: {human(self.done)} in {now - self.start:.0f}s ({human(rate)}/s)")


# ---------------------------------------------------------------- targets

class LocalTarget:
    """A folder that mirrors /data/prospero-win."""

    def __init__(self, root: Path):
        self.root = root
        self.name = str(root)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def size(self, rel: str) -> int | None:
        p = self.path(rel)
        return p.stat().st_size if p.is_file() else None

    def exists_dir(self, rel: str) -> bool:
        return self.path(rel).is_dir()

    def makedirs(self, rel: str) -> None:
        self.path(rel).mkdir(parents=True, exist_ok=True)

    def put_stream(self, rel: str, stream, progress: Progress | None = None) -> None:
        dest = self.path(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".azs-tmp")
        with tmp.open("wb") as out:
            while True:
                block = stream.read(CHUNK)
                if not block:
                    break
                out.write(block)
                if progress:
                    progress.add(len(block))
        os.replace(tmp, dest)

    def put_bytes(self, rel: str, data: bytes) -> None:
        self.put_stream(rel, io.BytesIO(data))

    def get_bytes(self, rel: str) -> bytes | None:
        p = self.path(rel)
        return p.read_bytes() if p.is_file() else None

    def hash(self, rel: str) -> str | None:
        p = self.path(rel)
        return sha256_file(p) if p.is_file() else None

    def delete(self, rel: str) -> None:
        p = self.path(rel)
        if p.is_file():
            p.unlink()

    def rmtree(self, rel: str) -> None:
        shutil.rmtree(self.path(rel), ignore_errors=True)

    def close(self) -> None:
        pass


class FtpTarget:
    """The console's FTP server (ftpsrv from ps5-payload-dev, port 2121 by default)."""

    def __init__(self, host: str, port: int, root: str):
        self.host, self.port, self.root = host, port, root.rstrip("/")
        self.name = f"ftp://{host}:{port}{self.root}"
        self.made: set[str] = set()
        self.connect()

    def connect(self) -> None:
        self.ftp = ftplib.FTP()
        try:
            self.ftp.connect(self.host, self.port, 30)
            self.ftp.login()
        except OSError as error:
            raise Failure(f"could not reach the PS5's FTP server at {self.host}:{self.port} ({error}). "
                          "Is the jailbreak running with ftpsrv?") from error
        # ftpsrv converts SELF containers on the fly unless told not to; a server
        # without the command answers 500 and converts nothing.
        for _ in range(2):
            try:
                reply = self.ftp.sendcmd("SELF")
            except ftplib.Error:
                break
            if "disabled" in reply.lower():
                break

    def abs(self, rel: str) -> str:
        return f"{self.root}/{rel}" if rel else self.root

    def size(self, rel: str) -> int | None:
        try:
            self.ftp.voidcmd("TYPE I")
            return self.ftp.size(self.abs(rel))
        except ftplib.error_perm:
            return None

    def exists_dir(self, rel: str) -> bool:
        try:
            self.ftp.cwd(self.abs(rel))
            return True
        except ftplib.error_perm:
            return False

    def makedirs(self, rel: str) -> None:
        parts = self.abs(rel).strip("/").split("/")
        for i in range(1, len(parts) + 1):
            directory = "/" + "/".join(parts[:i])
            if directory in self.made:
                continue
            try:
                self.ftp.mkd(directory)
            except ftplib.error_perm:
                pass
            self.made.add(directory)

    def put_stream(self, rel: str, stream, progress: Progress | None = None) -> None:
        self.makedirs(posixpath.dirname(rel))
        callback = (lambda block: progress.add(len(block))) if progress else None
        self.ftp.storbinary(f"STOR {self.abs(rel)}", stream, blocksize=CHUNK, callback=callback)

    def put_bytes(self, rel: str, data: bytes) -> None:
        self.put_stream(rel, io.BytesIO(data))

    def get_bytes(self, rel: str) -> bytes | None:
        out = io.BytesIO()
        try:
            self.ftp.retrbinary(f"RETR {self.abs(rel)}", out.write)
        except ftplib.error_perm:
            return None
        return out.getvalue()

    def hash(self, rel: str) -> str | None:
        digest = hashlib.sha256()
        try:
            self.ftp.retrbinary(f"RETR {self.abs(rel)}", digest.update, blocksize=CHUNK)
        except ftplib.error_perm:
            return None
        return digest.hexdigest()

    def delete(self, rel: str) -> None:
        try:
            # ftpsrv confirms a delete with 226, which ftplib's delete() refuses.
            self.ftp.sendcmd(f"DELE {self.abs(rel)}")
        except ftplib.error_perm:
            pass

    def listdir(self, rel: str) -> dict[str, str]:
        self.ftp.cwd(self.abs(rel))
        try:
            return {name: facts.get("type", "file") for name, facts in self.ftp.mlsd()
                    if name not in (".", "..")}
        except ftplib.error_perm:
            return {name: ("dir" if self.exists_dir(posixpath.join(rel, name)) else "file")
                    for name in (posixpath.basename(n) for n in self.ftp.nlst())
                    if name not in (".", "..")}

    def rmtree(self, rel: str) -> None:
        if not self.exists_dir(rel):
            return
        for name, kind in self.listdir(rel).items():
            child = posixpath.join(rel, name)
            if kind == "dir":
                self.rmtree(child)
            else:
                self.delete(child)
        try:
            self.ftp.rmd(self.abs(rel))
        except ftplib.error_perm:
            pass

    def close(self) -> None:
        try:
            self.ftp.quit()
        except (OSError, ftplib.Error, EOFError):
            pass


def open_target(args) -> LocalTarget | FtpTarget:
    if args.to:
        return LocalTarget(Path(args.to).expanduser())
    host = args.host or os.environ.get("PS5_HOST", "")
    if not host:
        raise Failure("give the PS5's address with --host (or a local folder with --to)")
    return FtpTarget(host, args.port, args.remote)


# ---------------------------------------------------------------- the game files

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def pe_machine(path: Path) -> int:
    with path.open("rb") as f:
        head = f.read(4096)
    if head[:2] != b"MZ":
        raise Failure(f"{path} is not a Windows program")
    pe = struct.unpack_from("<I", head, 0x3C)[0]
    if head[pe:pe + 4] != b"PE\0\0":
        raise Failure(f"{path} is not a Windows program")
    return struct.unpack_from("<H", head, pe + 4)[0]


def extract_rar(archive: Path, dest: Path) -> None:
    attempts = []
    for tool in ("bsdtar", "tar"):
        if shutil.which(tool):
            attempts.append([tool, "-xf", str(archive), "-C", str(dest)])
    if shutil.which("unrar"):
        attempts.append(["unrar", "x", "-o+", "-idq", str(archive), str(dest) + os.sep])
    for tool in ("7zz", "7z", "7za"):
        if shutil.which(tool):
            attempts.append([tool, "x", "-y", f"-o{dest}", str(archive)])
    if shutil.which("unar"):
        attempts.append(["unar", "-q", "-f", "-o", str(dest), str(archive)])
    for command in attempts:
        log(f"Extracting {archive.name} with {command[0]} ...")
        done = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if done.returncode == 0 and any(dest.rglob(EXE)):
            return
        log(f"  {command[0]} could not extract it: {done.stderr.decode(errors='replace').strip()[:200]}")
        for child in dest.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    raise Failure("could not extract the .rar. Install 7-Zip, unrar or unar, or extract it yourself "
                  "and give the folder instead.")


def find_game(folder: Path) -> Path:
    candidates = sorted(folder.rglob(EXE), key=lambda p: len(p.parts))
    for exe in candidates:
        if (exe.parent / "Data").is_dir():
            return exe.parent
    if candidates:
        raise Failure(f"found {candidates[0]} but no Data folder beside it")
    raise Failure(f"no {EXE} in {folder}")


def prepare_source(source: Path, work: Path) -> Path:
    """The folder that holds AzerothSurvivors.exe and Data/, extracting archives into work."""
    if not source.exists():
        raise Failure(f"{source} does not exist")
    if source.is_dir():
        game = find_game(source)
    else:
        kind = source.suffix.lower()
        if kind == ".zip":
            log(f"Extracting {source.name} ...")
            with zipfile.ZipFile(source) as z:
                z.extractall(work)
        elif kind == ".rar":
            extract_rar(source, work)
        elif source.name == EXE:
            return find_game(source.parent)
        else:
            raise Failure(f"{source}: give the .rar, a .zip or the game folder")
        game = find_game(work)
    machine = pe_machine(game / EXE)
    if machine != 0x8664:
        raise Failure(f"{game / EXE} is not the 64-bit Windows build (machine 0x{machine:x})")
    mpqs = [p for p in (game / "Data").iterdir() if p.suffix.lower() == ".mpq"]
    if not mpqs:
        raise Failure(f"{game / 'Data'} has no .MPQ files")
    return game


def game_files(game: Path) -> list[tuple[str, Path]]:
    """(path relative to the game folder, local path), the program last."""
    files = []
    for path in sorted(game.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(game).as_posix()
        top = rel.split("/")[0]
        # Skip what the game makes for itself (its updater's staging, the old exe it
        # keeps after an update, logs) and anything that is not the game.
        if top in (".update", "update") or rel.endswith((".old", ".azs-tmp")) or rel == "azs-ps5.json":
            continue
        if top != "Data" and rel != EXE:
            continue
        files.append((rel, path))
    files.sort(key=lambda item: item[0] == EXE)
    return files


def upload(target, rel: str, path: Path, force: bool = False) -> bool:
    """Sends path unless the target already has a file of that size (or force). True when sent."""
    size = path.stat().st_size
    if not force and target.size(rel) == size:
        log(f"  {posixpath.basename(rel)}: already there")
        return False
    progress = Progress(posixpath.basename(rel), size)
    with path.open("rb") as stream:
        target.put_stream(rel, stream, progress)
    progress.finish()
    stored = target.size(rel)
    if stored != size:
        raise Failure(f"{rel}: the console stored {stored} bytes instead of {size}; run the command again")
    return True


def read_marker(target) -> dict:
    data = target.get_bytes(MARKER)
    if not data:
        return {}
    try:
        return json.loads(data)
    except ValueError:
        return {}


def write_marker(target, marker: dict) -> None:
    target.put_bytes(MARKER, (json.dumps(marker, indent=1, sort_keys=True) + "\n").encode())


# ---------------------------------------------------------------- the Wine prefix

def prefix_manifest() -> dict:
    try:
        manifest = json.loads(PREFIX_MANIFEST.read_text())
    except (OSError, ValueError) as error:
        raise Failure(f"{PREFIX_MANIFEST} is missing or damaged: {error}") from error
    if PREFIX_URL:
        manifest["url"] = PREFIX_URL
    if not all(manifest.get(k) for k in ("url", "sha256", "size", "prospero_win")):
        raise Failure(f"{PREFIX_MANIFEST} is incomplete")
    return manifest


def fetch_prefix(manifest: dict) -> Path:
    """The prefix zip in the cache, downloaded and checked against the manifest."""
    cached = CACHE / "prefix" / f"{manifest['sha256']}.zip"
    if cached.is_file() and cached.stat().st_size == int(manifest["size"]):
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    log(f"Downloading the Wine prefix for prospero-win {manifest['prospero_win']} ({human(int(manifest['size']))})")
    download(manifest["url"], cached, manifest["sha256"])
    return cached


def prefix_complete(target) -> bool:
    return all(target.size(f"{PREFIX_DIR}/{name}") for name in REGISTRY) and \
        bool(target.size(f"{PREFIX_DIR}/drive_c/windows/system32/kernel32.dll"))


def install_prefix(target, reinstall: bool = False) -> None:
    """Puts the game's Wine prefix on the console. An existing whole prefix is kept
    (its registry holds the game's settings), only its game settings are checked."""
    if prefix_complete(target) and not reinstall:
        log(f"  the Wine prefix is already there ({PREFIX_DIR}); keeping it")
        return
    manifest = prefix_manifest()
    archive = fetch_prefix(manifest)
    with zipfile.ZipFile(archive) as z:
        members = [m for m in z.infolist() if not m.is_dir()]
        for m in z.infolist():
            if m.is_dir():
                target.makedirs(f"{PREFIX_DIR}/{m.filename.rstrip('/')}")
        # The registry last: a prefix with its registry is a whole one.
        members.sort(key=lambda m: m.filename in REGISTRY)
        total = sum(m.file_size for m in members)
        log(f"  {len(members)} files ({human(total)}) to {PREFIX_DIR}")
        progress = Progress("Wine prefix", total)
        sent = 0
        for m in members:
            rel = f"{PREFIX_DIR}/{m.filename}"
            if not reinstall and m.filename not in REGISTRY and target.size(rel) == m.file_size:
                progress.add(m.file_size)
                continue
            with z.open(m) as stream:
                target.put_stream(rel, stream, progress)
            sent += 1
        progress.finish()
        log(f"  sent {sent} files, {len(members) - sent} were already there")


def ensure_game_env(target) -> bool:
    """Makes sure HKCU\\Environment in the prefix's user.reg has GAME_ENV. True if changed."""
    rel = f"{PREFIX_DIR}/user.reg"
    data = target.get_bytes(rel)
    if data is None:
        raise Failure(f"{rel} is missing: the Wine prefix is not complete; run install again")
    reg = data.decode("utf-8")
    newline = "\r\n" if "\r\n" in reg else "\n"
    lines = reg.split(newline)
    head = next((i for i, l in enumerate(lines) if l.startswith("[Environment]")), None)
    want = {k: f'"{k}"="{v}"' for k, v in GAME_ENV.items()}
    if head is None:
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", f"[Environment] {int(time.time())}", *want.values(), ""]
    else:
        end = next((i for i in range(head + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        body = lines[head + 1:end]
        if all(line in body for line in want.values()):
            return False
        body = [l for l in body if not any(l.startswith(f'"{k}"=') for k in want)]
        while body and body[-1] == "":
            body.pop()
        lines[head + 1:end] = body + list(want.values()) + [""]
    target.put_bytes(rel, newline.join(lines).encode("utf-8"))
    log("  set the game's GPU settings in the prefix's registry")
    return True


def install_profile(target) -> None:
    profile = (PS5_FILES / "profiles" / f"{SLUG}.profile").read_bytes()
    preset = (PS5_FILES / "input" / f"{SLUG}.input").read_bytes()
    target.put_bytes(PRESET, preset)
    target.put_bytes(PROFILE, profile)
    log(f"  {PROFILE} and {PRESET}")
    listing = target.get_bytes(PROFILE_LIST)
    if listing is None:
        # Without profiles.lst the launcher lists every profile; writing one here
        # would hide the others.
        return
    lines = [line.strip() for line in listing.decode("latin-1").splitlines() if line.strip()]
    if f"{SLUG}.profile" in lines:
        return
    if len(lines) >= CATALOG_MAX:
        raise Failure(f"{PROFILE_LIST} already lists {len(lines)} games, the launcher's maximum "
                      f"({CATALOG_MAX}); take one out and run install again")
    target.put_bytes(PROFILE_LIST, ("\n".join(lines + [f"{SLUG}.profile"]) + "\n").encode("latin-1"))
    log(f"  added to {PROFILE_LIST}")


# ---------------------------------------------------------------- the official update channel

def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def http_json(url: str) -> dict:
    try:
        return json.loads(http_get(url))
    except (OSError, ValueError) as error:
        raise Failure(f"could not read {url}: {error}") from error


def fetch_item(base_url: str, item: dict, label: str) -> Path:
    """Downloads an item of latest.json / patches.json (gzip split into parts),
    checks its size and SHA-256 and returns the cached file."""
    sha, size = item["sha256"], int(item["size"])
    cached = CACHE / "items" / sha
    if cached.is_file() and cached.stat().st_size == size:
        return cached
    cached.parent.mkdir(parents=True, exist_ok=True)
    parts = item.get("parts") or []
    if not parts:
        raise Failure(f"{label}: the listing has no parts")
    packed = cached.with_suffix(".gz")
    progress = Progress(f"downloading {label}", sum(int(p["size"]) for p in parts))
    with packed.open("wb") as out:
        for part in parts:
            url = urllib.parse.urljoin(base_url, part["url"])
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            got = 0
            with urllib.request.urlopen(request, timeout=60) as response:
                for block in iter(lambda: response.read(1 << 20), b""):
                    out.write(block)
                    got += len(block)
                    progress.add(len(block))
            if got != int(part["size"]):
                raise Failure(f"{url}: got {got} bytes, expected {part['size']}")
    progress.finish()
    digest, total = hashlib.sha256(), 0
    tmp = cached.with_suffix(".tmp")
    with gzip.open(packed, "rb") as src, tmp.open("wb") as out:
        for block in iter(lambda: src.read(CHUNK), b""):
            digest.update(block)
            total += len(block)
            out.write(block)
    packed.unlink()
    if total != size or digest.hexdigest() != sha:
        tmp.unlink()
        raise Failure(f"{label}: the download is damaged (checksum mismatch)")
    os.replace(tmp, cached)
    return cached


def plan_update(target, marker: dict, check_exe_hash: bool) -> tuple[dict, list, list]:
    """(latest.json, [(rel, item, base) to send], [(rel, why) that are current])."""
    latest = http_json(UPDATE_URL)
    patches = http_json(DATA_URL)
    if not all(k in latest for k in ("version", "size", "sha256", "parts")):
        raise Failure("latest.json is incomplete")
    send, current = [], []
    for patch in patches.get("patches", []):
        rel = f"{GAME_DIR}/Data/{patch['name']}"
        if target.size(rel) == int(patch["size"]) and marker.get("data", {}).get(patch["name"]) in (None, patch["sha256"]):
            current.append((rel, "current"))
        else:
            send.append((rel, patch, DATA_URL))
    exe_rel = f"{GAME_DIR}/{EXE}"
    exe_size = target.size(exe_rel)
    exe_current = marker.get("exe_sha256") == latest["sha256"] and exe_size == int(latest["size"])
    if not exe_current and check_exe_hash and exe_size == int(latest["size"]):
        log("Checking the console's program against the latest version ...")
        exe_current = target.hash(exe_rel) == latest["sha256"]
    if exe_current:
        current.append((exe_rel, f"v{latest['version']}"))
    else:
        send.append((exe_rel, latest, UPDATE_URL))   # the program last
    return latest, send, current


def run_update(target, check_exe_hash: bool = True) -> None:
    marker = read_marker(target)
    latest, send, _ = plan_update(target, marker, check_exe_hash)
    if not send:
        log(f"Up to date: v{latest['version']}.")
        return
    log(f"Updating to v{latest['version']}: {len(send)} file(s)")
    for rel, item, base in send:
        local = fetch_item(base, item, posixpath.basename(rel))
        upload(target, rel, local, force=True)
        if rel.endswith(EXE):
            marker["exe_sha256"] = item["sha256"]
            marker["version"] = latest["version"]
        else:
            marker.setdefault("data", {})[posixpath.basename(rel)] = item["sha256"]
        write_marker(target, marker)
    if latest.get("notes"):
        log(f"What's new in v{latest['version']}: {latest['notes']}")
    log("Done. The console's copy is current.")


# ---------------------------------------------------------------- commands

def mirror_release(page_url: str) -> dict:
    """The .rar the mirror page offers: {url, name, sha256, version}. The page links a
    new /downloads/<id>/<name>.rar for each release and prints its SHA-256."""
    try:
        page = http_get(page_url).decode("utf-8", "replace")
    except OSError as error:
        raise Failure(f"could not read the mirror page {page_url}: {error}") from error
    link = re.search(r'href="([^"]+\.rar)"', page, re.I)
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    sha = re.search(r"SHA-?256\s*([0-9a-fA-F]{64})", text)
    if not link or not sha:
        raise Failure(f"the mirror page {page_url} doesn't list a .rar with its SHA-256")
    url = urllib.parse.urljoin(page_url, html.unescape(link.group(1)))
    version = re.search(r"Version:?\s*([0-9][\w.\-]*)", text)
    return {"url": url, "name": posixpath.basename(urllib.parse.urlparse(url).path),
            "sha256": sha.group(1).lower(), "version": version.group(1) if version else None}


def download(url: str, out: Path, sha: str | None = None) -> None:
    """Downloads url to out through out.part, picking up a .part left by an earlier try
    when the server takes byte ranges, and checks the SHA-256 when one is given."""
    tmp = out.with_name(out.name + ".part")
    have = tmp.stat().st_size if tmp.is_file() else 0
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        resumed = have and response.status == 206
        if have and not resumed:
            have = 0
        length = int(response.headers.get("Content-Length") or 0)
        progress = Progress(out.name, have + length)
        progress.done = have
        with tmp.open("ab" if resumed else "wb") as f:
            for block in iter(lambda: response.read(1 << 20), b""):
                f.write(block)
                progress.add(len(block))
    progress.finish()
    if resumed:
        log(f"  (carried on from {human(have)})")
    if sha:
        got = sha256_file(tmp)
        if got != sha:
            tmp.unlink()
            if resumed:
                # The piece from an earlier try was of another file; start over once.
                log("  the earlier partial download didn't match; starting over")
                return download(url, out, sha)
            raise Failure(f"{out.name}: the download is damaged (SHA-256 {got}, expected {sha})")
        log(f"  SHA-256 checked: {sha}")
    os.replace(tmp, out)


def cmd_fetch(args) -> None:
    dest = Path(args.dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    release = None
    if not args.official:
        try:
            release = mirror_release(MIRROR_PAGE)
        except Failure as error:
            log(f"The mirror is unavailable ({error}); using the official link, which is slower.")
    if release:
        url, sha = release["url"], release["sha256"]
        out = dest / release["name"]
        where = f"the mirror ({urllib.parse.urlparse(MIRROR_PAGE).netloc}), version {release['version'] or '?'}"
    else:
        url, sha = DOWNLOAD_URL, None
        out = dest / posixpath.basename(urllib.parse.urlparse(DOWNLOAD_URL).path)
        where = "the official link on wow.aethus.pl"
    if out.is_file() and not args.force:
        if sha and sha256_file(out) != sha:
            log(f"{out} is there but isn't the mirror's current version; downloading it again")
        else:
            log(f"{out} is already there (--force downloads it again)")
            return
    log(f"Downloading the Windows build from {where}")
    try:
        download(url, out, sha)
    except (OSError, Failure) as error:
        if not release:
            raise Failure(f"download failed: {error}") from error
        log(f"The mirror download failed ({error}); trying the official link, which is slower.")
        out = dest / posixpath.basename(urllib.parse.urlparse(DOWNLOAD_URL).path)
        stale = out.with_name(out.name + ".part")
        if stale.is_file():
            stale.unlink()      # the mirror's piece; the official link has no checksum to vouch for it
        download(DOWNLOAD_URL, out)
    log(f"Saved {out}. Next: azs_ps5.py install \"{out}\" --host <PS5 IP>")


def remove_legacy(target) -> None:
    """0.1 put the game into prospero-win's default prefix, which can't run it."""
    if target.exists_dir(LEGACY_GAME_DIR):
        target.rmtree(LEGACY_GAME_DIR)
        log(f"  removed the old copy in {LEGACY_GAME_DIR} (from an earlier version of this tool)")


def cmd_install(args) -> None:
    source = Path(args.source).expanduser()
    with tempfile.TemporaryDirectory(prefix="azs-ps5-", dir=args.workdir) as work:
        game = prepare_source(source, Path(work))
        files = game_files(game)
        total = sum(p.stat().st_size for _, p in files)
        target = open_target(args)
        try:
            log("The Wine prefix (the game's C: drive and registry):")
            install_prefix(target, reinstall=args.reinstall_prefix)
            ensure_game_env(target)
            log(f"The game: {len(files)} files ({human(total)}) from {game}")
            log(f"  to {target.name}/{GAME_DIR}")
            for rel, path in files:
                upload(target, f"{GAME_DIR}/{rel}", path)
            marker = read_marker(target)
            marker["exe_sha256"] = sha256_file(game / EXE)
            marker.pop("version", None)
            marker["data"] = {}
            write_marker(target, marker)
            log("The launcher profile and controller preset:")
            install_profile(target)
            remove_legacy(target)
            if args.no_update:
                log("Skipped the update check (--no-update).")
            else:
                try:
                    run_update(target, check_exe_hash=False)
                except Failure as error:
                    log(f"Could not update to the latest version now ({error}). "
                        "The game can update itself on the console, or run `update` later.")
        finally:
            target.close()
    log("Installed. Start prospero-win on the PS5 and pick Azeroth Survivors.")


def cmd_update(args) -> None:
    target = open_target(args)
    try:
        if target.size(f"{GAME_DIR}/{EXE}") is None:
            raise Failure("Azeroth Survivors is not installed there; run install first")
        run_update(target)
    finally:
        target.close()


def cmd_status(args) -> None:
    target = open_target(args)
    try:
        exe = target.size(f"{GAME_DIR}/{EXE}")
        if exe is None:
            log("Azeroth Survivors is not installed.")
            return
        log(f"Installed in {target.name}/{GAME_DIR}")
        log(f"  Wine prefix: {'yes' if prefix_complete(target) else 'INCOMPLETE (run install again)'}")
        log(f"  profile: {'yes' if target.size(PROFILE) is not None else 'MISSING'}, "
            f"preset: {'yes' if target.size(PRESET) is not None else 'MISSING'}")
        marker = read_marker(target)
        if args.offline:
            log(f"  version: {marker.get('version', 'unknown')}")
            return
        latest, send, _ = plan_update(target, marker, check_exe_hash=not args.quick)
        if send:
            log(f"  update available: v{latest['version']} ({len(send)} file(s): "
                + ", ".join(posixpath.basename(rel) for rel, _, _ in send) + ")")
        else:
            log(f"  up to date: v{latest['version']}")
    finally:
        target.close()


def cmd_uninstall(args) -> None:
    target = open_target(args)
    try:
        target.rmtree(PREFIX_DIR)          # the game, its prefix and the settings in it
        target.rmtree(LEGACY_GAME_DIR)
        target.delete(PROFILE)
        target.delete(PRESET)
        listing = target.get_bytes(PROFILE_LIST)
        if listing is not None:
            lines = [l.strip() for l in listing.decode("latin-1").splitlines() if l.strip()]
            if f"{SLUG}.profile" in lines:
                lines.remove(f"{SLUG}.profile")
                target.put_bytes(PROFILE_LIST, ("\n".join(lines) + "\n").encode("latin-1"))
        log("Removed Azeroth Survivors, its Wine prefix, its profile and its preset.")
    finally:
        target.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def target_args(p):
        where = p.add_mutually_exclusive_group()
        where.add_argument("--host", help="the PS5's IP address (or set PS5_HOST)")
        where.add_argument("--to", metavar="DIR", help="write into a local folder that mirrors /data/prospero-win")
        p.add_argument("--port", type=int, default=2121, help="the PS5's FTP port (default 2121)")
        p.add_argument("--remote", default="/data/prospero-win", help="prospero-win's library on the PS5")

    p = sub.add_parser("fetch", help="download the official Windows build")
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--force", action="store_true", help="download again even if the file is there")
    p.add_argument("--official", action="store_true",
                   help="use the official link on wow.aethus.pl instead of the (much faster) mirror")
    p.set_defaults(run=cmd_fetch)

    p = sub.add_parser("install", help="copy the game to the PS5")
    p.add_argument("source", help="AzerothSurvivorsWindowsNew.rar, a .zip, or the game folder")
    p.add_argument("--no-update", action="store_true", help="don't bring it to the latest version afterwards")
    p.add_argument("--reinstall-prefix", action="store_true",
                   help="send the Wine prefix again even if the console has one (resets the game's settings)")
    p.add_argument("--workdir", help="where to extract the archive (default: the system's temp folder)")
    target_args(p)
    p.set_defaults(run=cmd_install)

    p = sub.add_parser("update", help="update the PS5's copy to the latest official version")
    target_args(p)
    p.set_defaults(run=cmd_update)

    p = sub.add_parser("status", help="show what is installed")
    p.add_argument("--offline", action="store_true", help="don't ask the update server")
    p.add_argument("--quick", action="store_true", help="trust sizes instead of hashing the console's program")
    target_args(p)
    p.set_defaults(run=cmd_status)

    p = sub.add_parser("uninstall", help="remove the game from the PS5")
    target_args(p)
    p.set_defaults(run=cmd_uninstall)

    args = parser.parse_args(argv)
    try:
        args.run(args)
    except Failure as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped; run the same command again to carry on", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
