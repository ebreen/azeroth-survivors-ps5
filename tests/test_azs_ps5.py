"""Tests for azs_ps5.py: a local folder, a fake update server and (with pyftpdlib) a fake PS5 FTP server."""
import contextlib
import gzip
import hashlib
import http.server
import io
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import azs_ps5  # noqa: E402

GAME = azs_ps5.GAME_DIR


def fake_exe(tag: bytes, machine: int = 0x8664) -> bytes:
    head = bytearray(0x200)
    head[0:2] = b"MZ"
    head[0x3C:0x40] = (0x80).to_bytes(4, "little")
    head[0x80:0x84] = b"PE\0\0"
    head[0x84:0x86] = machine.to_bytes(2, "little")
    return bytes(head) + tag * 1000


def make_game(folder: Path, exe=b"old", patches=None) -> Path:
    game = folder / "AzerothSurvivors"
    (game / "Data").mkdir(parents=True)
    (game / azs_ps5.EXE).write_bytes(fake_exe(exe))
    for name, data in (patches or {"patch.MPQ": b"base" * 5000, "patch-2.MPQ": b"two" * 100}).items():
        (game / "Data" / name).write_bytes(data)
    return game


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        result = fn(*args, **kwargs)
    return result, out.getvalue()


class UpdateServer:
    """Serves latest.json and patches.json the way wow.aethus.pl does: each item
    gzipped and cut into parts."""

    def __init__(self, root: Path, exe: bytes, version: str, patches: dict, part_size=700, corrupt=False):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        latest = dict(self.item(exe, version, part_size), version=version, notes="test notes")
        (root / "latest.json").write_text(json.dumps(latest))
        (root / "data").mkdir(exist_ok=True)
        listing = []
        for name, data in patches.items():
            listing.append(dict(self.item(data, f"data/{name}", part_size, rel_to="data/"), name=name))
        (root / "data" / "patches.json").write_text(json.dumps({"patches": listing}))
        if corrupt:
            latest["sha256"] = "0" * 64
            (root / "latest.json").write_text(json.dumps(latest))
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(root), **k)
        http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.httpd.server_port}/"
        azs_ps5.UPDATE_URL = base + "latest.json"
        azs_ps5.DATA_URL = base + "data/patches.json"

    def item(self, data: bytes, folder: str, part_size: int, rel_to: str = "") -> dict:
        packed = gzip.compress(data)
        parts = []
        (self.root / folder).mkdir(parents=True, exist_ok=True)
        for i in range(0, len(packed), part_size):
            name = f"part-{i // part_size:03d}.bin"
            (self.root / folder / name).write_bytes(packed[i:i + part_size])
            parts.append({"url": f"{folder[len(rel_to):]}/{name}", "size": len(packed[i:i + part_size])})
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "parts": parts}

    def close(self):
        self.httpd.shutdown()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="azs-test-"))
        os.environ["AZS_PS5_CACHE"] = str(self.tmp / "cache")
        azs_ps5.CACHE = self.tmp / "cache"
        self.console = self.tmp / "console"
        self.servers = []
        self.serve_prefix()

    PREFIX_FILES = {
        "system.reg": b"WINE REGISTRY Version 2\n\n[Software\\\\Microsoft\\\\Wow64\\\\x86] 1\n@=\"wowprospero.dll\"\n",
        "user.reg": b"WINE REGISTRY Version 2\n\n[Environment] 1\n\"TEMP\"=\"C:\\\\users\\\\prospero\\\\Temp\"\n\n[Keyboard Layout] 1\n\"1\"=\"00000409\"\n",
        "userdef.reg": b"WINE REGISTRY Version 2\n",
        "dosdevices/.pw-symlinks": b"c:\t../drive_c\n",
        "drive_c/windows/system32/kernel32.dll": b"MZ kernel32" * 100,
        "drive_c/windows/syswow64/kernel32.dll": b"MZ kernel32 i386" * 100,
    }

    def serve_prefix(self, files=None):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("drive_c/users/prospero/Documents/", b"")
            for name, data in (files or self.PREFIX_FILES).items():
                z.writestr(name, data)
        blob = buf.getvalue()
        server = RangeServer({"/prefix.zip": blob})
        self.servers.append(server)
        manifest = self.tmp / "prefix.json"
        manifest.write_text(json.dumps({"prospero_win": "v0.2.0", "url": server.base + "/prefix.zip",
                                        "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}))
        azs_ps5.PREFIX_MANIFEST = manifest
        azs_ps5.PREFIX_URL = ""
        self.prefix_server = server

    def tearDown(self):
        for s in self.servers:
            s.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv):
        code, out = quiet(azs_ps5.main, list(argv))
        return code, out

    def serve(self, **kwargs):
        server = UpdateServer(self.tmp / "www", **kwargs)
        self.servers.append(server)
        return server


class Prefix(Base):
    def test_install_puts_the_prefix_and_the_gpu_settings(self):
        code, out = self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 0, out)
        prefix = self.console / azs_ps5.PREFIX_DIR
        self.assertTrue((prefix / "drive_c/windows/system32/kernel32.dll").is_file())
        self.assertTrue((prefix / "drive_c/users/prospero/Documents").is_dir())
        self.assertTrue((prefix / "dosdevices/.pw-symlinks").is_file())
        reg = (prefix / "user.reg").read_text()
        env = reg[reg.index("[Environment]"):reg.index("[Keyboard Layout]")]
        self.assertIn('"WGPU_ALLOW_UNDERLYING_NONCOMPLIANT_ADAPTER"="1"', env)
        self.assertIn('"WGPU_BACKEND"="vulkan"', env)
        self.assertIn('"TEMP"=', env)
        self.assertTrue(env.endswith("\n\n"))                       # sections stay apart
        self.assertTrue((prefix / "drive_c/Games/AzerothSurvivors/AzerothSurvivors.exe").is_file())
        self.assertIn("prefix = azeroth-survivors", (self.console / azs_ps5.PROFILE).read_text())

    def test_existing_prefix_and_its_settings_are_kept(self):
        self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console), "--no-update")
        reg = self.console / azs_ps5.PREFIX_DIR / "user.reg"
        reg.write_text(reg.read_text() + "\n[Software\\\\Game] 2\n\"saved\"=\"yes\"\n")
        hits = len(self.prefix_server.hits)
        code, out = self.run_cli("install", str(make_game(self.tmp / "src2")), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 0, out)
        self.assertIn("keeping it", out)
        self.assertIn('"saved"="yes"', reg.read_text())
        self.assertEqual(reg.read_text().count('"WGPU_BACKEND"'), 1)
        self.assertEqual(len(self.prefix_server.hits), hits)          # nothing downloaded again

    def test_old_settings_get_the_gpu_fix(self):
        files = dict(self.PREFIX_FILES)
        files["user.reg"] = b"WINE REGISTRY Version 2\n\n[Keyboard Layout] 1\n"   # no [Environment]
        self.serve_prefix(files)
        code, out = self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 0, out)
        reg = (self.console / azs_ps5.PREFIX_DIR / "user.reg").read_text()
        self.assertIn('[Environment]', reg)
        self.assertIn('"WGPU_ALLOW_UNDERLYING_NONCOMPLIANT_ADAPTER"="1"', reg)

    def test_damaged_prefix_download_is_refused(self):
        self.prefix_server.files["/prefix.zip"] = b"not the zip" * 50
        code, out = self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 1)
        self.assertFalse((self.console / azs_ps5.PREFIX_DIR / "user.reg").exists())

    def test_removes_the_copy_an_earlier_version_left_in_the_default_prefix(self):
        legacy = self.console / azs_ps5.LEGACY_GAME_DIR
        legacy.mkdir(parents=True)
        (legacy / "AzerothSurvivors.exe").write_bytes(b"old")
        (self.console / "prefix/dosdevices").mkdir(parents=True)
        code, out = self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 0, out)
        self.assertFalse(legacy.exists())
        self.assertTrue((self.console / "prefix/dosdevices").is_dir())   # the rest of the default prefix stays

    def test_uninstall_removes_the_prefix(self):
        self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console), "--no-update")
        code, out = self.run_cli("uninstall", "--to", str(self.console))
        self.assertEqual(code, 0, out)
        self.assertFalse((self.console / azs_ps5.PREFIX_DIR).exists())

    def test_the_shipped_manifest_is_complete(self):
        manifest = json.loads((ROOT / "ps5/prefix.json").read_text())
        for key in ("prospero_win", "url", "sha256", "size"):
            self.assertTrue(manifest.get(key), key)
        self.assertRegex(manifest["sha256"], r"^[0-9a-f]{64}$")


class LocalInstall(Base):
    def test_folder_install_layout(self):
        game = make_game(self.tmp / "src")
        code, out = self.run_cli("install", str(game), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.console / GAME / azs_ps5.EXE).read_bytes(), (game / azs_ps5.EXE).read_bytes())
        self.assertTrue((self.console / GAME / "Data" / "patch.MPQ").is_file())
        self.assertEqual((self.console / azs_ps5.PROFILE).read_bytes(),
                         (ROOT / "ps5/profiles/azeroth-survivors.profile").read_bytes())
        self.assertTrue((self.console / azs_ps5.PRESET).is_file())
        # No profiles.lst on the console: none is made (it would hide the other games).
        self.assertFalse((self.console / azs_ps5.PROFILE_LIST).exists())
        marker = json.loads((self.console / azs_ps5.MARKER).read_text())
        self.assertEqual(marker["exe_sha256"], hashlib.sha256((game / azs_ps5.EXE).read_bytes()).hexdigest())

    def test_skips_updater_leftovers_and_resumes(self):
        game = make_game(self.tmp / "src")
        (game / ".update").mkdir()
        (game / ".update" / "junk").write_bytes(b"x")
        (game / "AzerothSurvivors.old").write_bytes(b"x")
        code, out = self.run_cli("install", str(game), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.console / GAME / ".update").exists())
        self.assertFalse((self.console / GAME / "AzerothSurvivors.old").exists())
        code, out = self.run_cli("install", str(game), "--to", str(self.console), "--no-update")
        self.assertIn("already there", out)

    def test_zip_source(self):
        game = make_game(self.tmp / "src")
        archive = self.tmp / "game.zip"
        with zipfile.ZipFile(archive, "w") as z:
            for p in game.rglob("*"):
                z.write(p, p.relative_to(game.parent))
        code, out = self.run_cli("install", str(archive), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.console / GAME / azs_ps5.EXE).is_file())

    def test_refuses_32bit_and_missing_data(self):
        game = make_game(self.tmp / "a")
        (game / azs_ps5.EXE).write_bytes(fake_exe(b"x", machine=0x14C))
        code, _ = self.run_cli("install", str(game), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 1)
        game = make_game(self.tmp / "b")
        for p in (game / "Data").iterdir():
            p.unlink()
        code, _ = self.run_cli("install", str(game), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 1)

    def test_profiles_list(self):
        lst = self.console / azs_ps5.PROFILE_LIST
        lst.parent.mkdir(parents=True)
        lst.write_text("minesweeper.profile\npinball.profile\n")
        game = make_game(self.tmp / "src")
        self.run_cli("install", str(game), "--to", str(self.console), "--no-update")
        self.assertEqual(lst.read_text().split(), ["minesweeper.profile", "pinball.profile", "azeroth-survivors.profile"])
        self.run_cli("install", str(game), "--to", str(self.console), "--no-update")
        self.assertEqual(lst.read_text().split().count("azeroth-survivors.profile"), 1)
        self.run_cli("uninstall", "--to", str(self.console))
        self.assertEqual(lst.read_text().split(), ["minesweeper.profile", "pinball.profile"])
        self.assertFalse((self.console / GAME).exists())
        self.assertFalse((self.console / azs_ps5.PROFILE).exists())

    def test_full_profiles_list_is_refused(self):
        lst = self.console / azs_ps5.PROFILE_LIST
        lst.parent.mkdir(parents=True)
        lst.write_text("".join(f"g{i}.profile\n" for i in range(16)))
        code, _ = self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console), "--no-update")
        self.assertEqual(code, 1)


class Updates(Base):
    def test_update_brings_exe_and_new_data(self):
        game = make_game(self.tmp / "src")
        patches = {"patch-2.MPQ": b"two" * 100, "patch-A.MPQ": b"new data" * 300}
        new_exe = fake_exe(b"new")
        self.serve(exe=new_exe, version="9.9.9", patches=patches)
        code, out = self.run_cli("install", str(game), "--to", str(self.console))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.console / GAME / azs_ps5.EXE).read_bytes(), new_exe)
        self.assertEqual((self.console / GAME / "Data/patch-A.MPQ").read_bytes(), patches["patch-A.MPQ"])
        self.assertIn("test notes", out)
        marker = json.loads((self.console / azs_ps5.MARKER).read_text())
        self.assertEqual(marker["version"], "9.9.9")
        code, out = self.run_cli("update", "--to", str(self.console))
        self.assertIn("Up to date: v9.9.9", out)
        code, out = self.run_cli("status", "--to", str(self.console))
        self.assertIn("up to date: v9.9.9", out)

    def test_exe_updated_by_the_game_itself_is_recognised(self):
        game = make_game(self.tmp / "src")
        new_exe = fake_exe(b"new")
        self.serve(exe=new_exe, version="2.0.0", patches={})
        self.run_cli("install", str(game), "--to", str(self.console), "--no-update")
        # the in-game updater replaced the program on the console
        (self.console / GAME / azs_ps5.EXE).write_bytes(new_exe)
        code, out = self.run_cli("update", "--to", str(self.console))
        self.assertIn("Up to date: v2.0.0", out)

    def test_damaged_download_is_refused(self):
        self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console), "--no-update")
        before = (self.console / GAME / azs_ps5.EXE).read_bytes()
        self.serve(exe=fake_exe(b"new"), version="3.0.0", patches={}, corrupt=True)
        code, out = self.run_cli("update", "--to", str(self.console))
        self.assertEqual(code, 1)
        self.assertEqual((self.console / GAME / azs_ps5.EXE).read_bytes(), before)

    def test_offline_install_still_succeeds(self):
        azs_ps5.UPDATE_URL = "http://127.0.0.1:9/latest.json"
        azs_ps5.DATA_URL = "http://127.0.0.1:9/patches.json"
        code, out = self.run_cli("install", str(make_game(self.tmp / "src")), "--to", str(self.console))
        self.assertEqual(code, 0, out)
        self.assertIn("Could not update", out)


class RangeServer:
    """Serves {path: bytes} with byte ranges (like the mirror's Cloudflare front),
    counting the requests it gets."""

    def __init__(self, files: dict):
        self.files, self.hits = files, []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append((self.path, self.headers.get("Range")))
                body = outer.files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng[6:].split("-")[0])
                    self.send_response(206)
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(body) - start))
                self.end_headers()
                self.wfile.write(body[start:])

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()


def mirror_page(href: str, sha: str, version="0.115.0") -> bytes:
    return f"""<html><body><main><h1>Azeroth Survivors</h1>
<p class="filename">AzerothSurvivorsWindowsNew.rar</p><p class="version">Version: {version}</p>
<a class="button" href="{href}">Download</a>
<dl class="checksums"><dt>SHA-256</dt><dd><code>{sha}</code></dd><dt>MD5</dt><dd><code>x</code></dd></dl>
</main></body></html>""".encode()


class Fetch(Base):
    RAR = b"Rar!\x1a\x07\x01\x00" + bytes(range(256)) * 400

    def start(self, page=True, official=True, mirror_body=None):
        sha = hashlib.sha256(self.RAR).hexdigest()
        files = {}
        if page:
            files["/"] = mirror_page("/downloads/123-abc/AzerothSurvivorsWindowsNew.rar", sha)
        files["/downloads/123-abc/AzerothSurvivorsWindowsNew.rar"] = mirror_body if mirror_body is not None else self.RAR
        if official:
            files["/download/AzerothSurvivorsWindowsNew.rar"] = self.RAR
        server = RangeServer(files)
        self.servers.append(server)
        azs_ps5.MIRROR_PAGE = server.base + "/"
        azs_ps5.DOWNLOAD_URL = server.base + "/download/AzerothSurvivorsWindowsNew.rar"
        self.out = self.tmp / "dl"
        return server

    def test_mirror_is_used_and_checked(self):
        server = self.start()
        code, out = self.run_cli("fetch", str(self.out))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.out / "AzerothSurvivorsWindowsNew.rar").read_bytes(), self.RAR)
        self.assertIn("SHA-256 checked", out)
        self.assertIn("version 0.115.0", out)
        self.assertNotIn("/download/AzerothSurvivorsWindowsNew.rar", [p for p, _ in server.hits])
        code, out = self.run_cli("fetch", str(self.out))
        self.assertIn("already there", out)

    def test_resumes_a_partial_download(self):
        server = self.start()
        self.out.mkdir()
        (self.out / "AzerothSurvivorsWindowsNew.rar.part").write_bytes(self.RAR[:30000])
        code, out = self.run_cli("fetch", str(self.out))
        self.assertEqual(code, 0, out)
        self.assertIn(("/downloads/123-abc/AzerothSurvivorsWindowsNew.rar", "bytes=30000-"), server.hits)
        self.assertEqual((self.out / "AzerothSurvivorsWindowsNew.rar").read_bytes(), self.RAR)

    def test_stale_partial_from_another_version_starts_over(self):
        self.start()
        self.out.mkdir()
        (self.out / "AzerothSurvivorsWindowsNew.rar.part").write_bytes(b"something else" * 100)
        code, out = self.run_cli("fetch", str(self.out))
        self.assertEqual(code, 0, out)
        self.assertIn("starting over", out)
        self.assertEqual((self.out / "AzerothSurvivorsWindowsNew.rar").read_bytes(), self.RAR)

    def test_damaged_mirror_file_falls_back_to_official(self):
        server = self.start(mirror_body=b"corrupted" * 1000)
        code, out = self.run_cli("fetch", str(self.out))
        self.assertEqual(code, 0, out)
        self.assertIn("trying the official link", out)
        self.assertIn(("/download/AzerothSurvivorsWindowsNew.rar", None), server.hits)
        self.assertEqual((self.out / "AzerothSurvivorsWindowsNew.rar").read_bytes(), self.RAR)

    def test_mirror_page_down_falls_back_to_official(self):
        self.start(page=False)
        code, out = self.run_cli("fetch", str(self.out))
        self.assertEqual(code, 0, out)
        self.assertIn("mirror is unavailable", out)
        self.assertEqual((self.out / "AzerothSurvivorsWindowsNew.rar").read_bytes(), self.RAR)

    def test_official_flag(self):
        server = self.start()
        code, out = self.run_cli("fetch", str(self.out), "--official")
        self.assertEqual(code, 0, out)
        self.assertEqual([p for p, _ in server.hits], ["/download/AzerothSurvivorsWindowsNew.rar"])

    def test_parses_the_real_page_layout(self):
        # tests/fixtures/mirror-page.html is the mirror's page as served on 2026-10-26.
        page = (ROOT / "tests/fixtures/mirror-page.html").read_bytes()
        server = RangeServer({"/": page})
        self.servers.append(server)
        release = azs_ps5.mirror_release(server.base + "/")
        self.assertEqual(release["sha256"], "971db7968dc37ec50b18d64a1de7f693e137f6793a7368e4d5c27bd9522eccd8")
        self.assertEqual(release["version"], "0.115.0")
        self.assertEqual(release["name"], "AzerothSurvivorsWindowsNew.rar")
        self.assertTrue(release["url"].startswith(server.base + "/downloads/"))

try:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import ThreadedFTPServer
except ImportError:
    DummyAuthorizer = None


@unittest.skipIf(DummyAuthorizer is None, "pyftpdlib is not installed")
class OverFtp(Base):
    """The same against an FTP server rooted like the console's filesystem."""

    def setUp(self):
        super().setUp()
        self.fs = self.tmp / "ftproot"
        (self.fs / "data/prospero-win").mkdir(parents=True)
        auth = DummyAuthorizer()
        auth.add_anonymous(str(self.fs), perm="elradfmwMT")
        handler = type("H", (FTPHandler,), {"authorizer": auth})
        import logging
        logging.getLogger("pyftpdlib").setLevel(logging.ERROR)
        self.ftpd = ThreadedFTPServer(("127.0.0.1", 0), handler)
        self.port = self.ftpd.address[1]
        threading.Thread(target=self.ftpd.serve_forever, kwargs={"handle_exit": False}, daemon=True).start()

    def tearDown(self):
        self.ftpd.close_all()
        super().tearDown()

    def test_install_update_uninstall(self):
        lib = self.fs / "data/prospero-win"
        (lib / "profiles").mkdir()
        (lib / "profiles/profiles.lst").write_text("minesweeper.profile\n")
        new_exe = fake_exe(b"new")
        self.serve(exe=new_exe, version="5.0.0", patches={"patch-A.MPQ": b"A" * 5000})
        ftp = ["--host", "127.0.0.1", "--port", str(self.port)]
        code, out = self.run_cli("install", str(make_game(self.tmp / "src")), *ftp)
        self.assertEqual(code, 0, out)
        self.assertEqual((lib / GAME / azs_ps5.EXE).read_bytes(), new_exe)
        self.assertTrue((lib / GAME / "Data/patch-A.MPQ").is_file())
        self.assertTrue((lib / GAME / "Data/patch.MPQ").is_file())
        self.assertIn("azeroth-survivors.profile", (lib / "profiles/profiles.lst").read_text())
        code, out = self.run_cli("status", *ftp)
        self.assertIn("up to date: v5.0.0", out)
        code, out = self.run_cli("uninstall", *ftp)
        self.assertEqual(code, 0, out)
        self.assertFalse((lib / GAME).exists())
        self.assertEqual((lib / "profiles/profiles.lst").read_text().split(), ["minesweeper.profile"])


if __name__ == "__main__":
    unittest.main()
