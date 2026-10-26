#!/usr/bin/env python3
"""Put Azeroth Survivors (the Windows build) on a jailbroken PS5 running prospero-win.

    azs_ps5.py fetch [DIR]                     download the official Windows build (.rar)
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
import io
import json
import os
import posixpath
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
GAME_DIR = "prefix/drive_c/Games/AzerothSurvivors"      # the default prefix's C:\Games\AzerothSurvivors
PROFILE = f"profiles/{SLUG}.profile"
PROFILE_LIST = "profiles/profiles.lst"
PRESET = f"input/{SLUG}.input"
MARKER = f"{GAME_DIR}/azs-ps5.json"                      # what this tool last put there
CATALOG_MAX = 16                                          # the launcher drops profiles.lst above this

# The game's own update channel (the same URLs the game's updater reads, overridable
# the same way: WOW_ROGUE_UPDATE_URL / WOW_ROGUE_DATA_URL).
DOWNLOAD_URL = "https://wow.aethus.pl/download/AzerothSurvivorsWindowsNew.rar"
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

def cmd_fetch(args) -> None:
    dest = Path(args.dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / posixpath.basename(urllib.parse.urlparse(DOWNLOAD_URL).path)
    if out.is_file() and not args.force:
        log(f"{out} is already there (--force downloads it again)")
        return
    log(f"Downloading the official Windows build from {DOWNLOAD_URL}")
    request = urllib.request.Request(DOWNLOAD_URL, headers={"User-Agent": USER_AGENT})
    tmp = out.with_suffix(".part")
    with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as f:
        progress = Progress(out.name, int(response.headers.get("Content-Length") or 0))
        for block in iter(lambda: response.read(1 << 20), b""):
            f.write(block)
            progress.add(len(block))
    progress.finish()
    os.replace(tmp, out)
    log(f"Saved {out}. Next: azs_ps5.py install \"{out}\" --host <PS5 IP>")


def cmd_install(args) -> None:
    source = Path(args.source).expanduser()
    with tempfile.TemporaryDirectory(prefix="azs-ps5-", dir=args.workdir) as work:
        game = prepare_source(source, Path(work))
        files = game_files(game)
        total = sum(p.stat().st_size for _, p in files)
        target = open_target(args)
        try:
            log(f"Installing {len(files)} files ({human(total)}) from {game}")
            log(f"  to {target.name}/{GAME_DIR}")
            for rel, path in files:
                upload(target, f"{GAME_DIR}/{rel}", path)
            marker = read_marker(target)
            marker["exe_sha256"] = sha256_file(game / EXE)
            marker.pop("version", None)
            marker["data"] = {}
            write_marker(target, marker)
            log("Adding the launcher profile and controller preset:")
            install_profile(target)
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
        target.rmtree(GAME_DIR)
        target.delete(PROFILE)
        target.delete(PRESET)
        listing = target.get_bytes(PROFILE_LIST)
        if listing is not None:
            lines = [l.strip() for l in listing.decode("latin-1").splitlines() if l.strip()]
            if f"{SLUG}.profile" in lines:
                lines.remove(f"{SLUG}.profile")
                target.put_bytes(PROFILE_LIST, ("\n".join(lines) + "\n").encode("latin-1"))
        log("Removed Azeroth Survivors, its profile and its preset.")
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
    p.add_argument("--force", action="store_true")
    p.set_defaults(run=cmd_fetch)

    p = sub.add_parser("install", help="copy the game to the PS5")
    p.add_argument("source", help="AzerothSurvivorsWindowsNew.rar, a .zip, or the game folder")
    p.add_argument("--no-update", action="store_true", help="don't bring it to the latest version afterwards")
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
