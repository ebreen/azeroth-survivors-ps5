#!/bin/sh
# Builds the console-ready Wine prefix for Azeroth Survivors, without the game:
#
#   tools/build_prefix.sh PROSPERO_TAG OUT_DIR
#
# Runs on x86-64 Linux with WineHQ's wine-devel of the version prospero-win's
# release is built from (11.17 for v0.2.0), xvfb-run, python3 + PyYAML and
# pyftpdlib. Steps:
#   1. prospero-win's release zip (checked against its SHA256SUMS) for its Wine
#      PE modules and wowprospero.dll, and its source at the same tag for
#      tools/pw_install.py and tools/pw_prefix.py;
#   2. pw_install.py runs recipes/azeroth-survivors.yml as user "prospero" on a
#      placeholder game (a 64-bit stub exe), then the placeholder is removed;
#   3. every module in the prefix's system32/syswow64 is replaced by the copy
#      the console's runtime ships, so the prefix matches the PS5 exactly
#      (prospero-win patches ntdll, win32u, kernelbase, xinput, winevulkan...);
#   4. pw_prefix.py pushes it to a local FTP server laid out like the console,
#      which applies the console's registry and symlink conventions;
#   5. the console layout is zipped: OUT_DIR/azeroth-survivors-prefix-<tag>.zip
#      plus prefix.json (size, SHA-256, file count).
set -eu
tag=${1:?prospero-win tag, e.g. v0.2.0}
out=${2:?output folder}
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
wine=${WINE:-/opt/wine-devel/bin/wine}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$out"
out=$(CDPATH= cd -- "$out" && pwd)

echo "== prospero-win $tag"
base=https://github.com/mpereiraesaa/prospero-win/releases/download/$tag
curl -fsSL -o "$work/SHA256SUMS" "$base/SHA256SUMS"
curl -fsSL -o "$work/prospero-win-$tag.zip" "$base/prospero-win-$tag.zip"
(cd "$work" && grep " prospero-win-$tag.zip\$" SHA256SUMS | sha256sum -c -)
unzip -q "$work/prospero-win-$tag.zip" -d "$work/release"
runtime=$(find "$work/release" -type d -path '*/win/wine/lib/wine' | head -1)
[ -d "$runtime/x86_64-windows" ] || { echo "no Wine runtime in the release zip" >&2; exit 1; }
git clone -q --depth 1 --branch "$tag" https://github.com/mpereiraesaa/prospero-win "$work/pw" 2>/dev/null ||
    { git clone -q https://github.com/mpereiraesaa/prospero-win "$work/pw" && git -C "$work/pw" checkout -q "$tag"; }
echo "wine: $("$wine" --version), pinned: $(git -C "$work/pw" show HEAD:tools/build_wine_ps5.sh | sed -n 's/^WINE_COMMIT=//p')"

echo "== the prefix"
mkdir -p "$work/placeholder/Data"
python3 - "$work/placeholder/AzerothSurvivors.exe" <<'PY'
import sys
head = bytearray(0x200); head[0:2] = b"MZ"; head[0x3C:0x40] = (0x80).to_bytes(4, "little")
head[0x80:0x84] = b"PE\0\0"; head[0x84:0x86] = (0x8664).to_bytes(2, "little")
head[0x98:0x9A] = (0x20B).to_bytes(2, "little")   # optional header magic: PE32+
open(sys.argv[1], "wb").write(bytes(head))
PY
library=$work/library
xvfb-run -a python3 "$work/pw/tools/pw_install.py" "$here/recipes/azeroth-survivors.yml" \
    --library "$library" --wine "$wine" --file game="$work/placeholder"
prefix=$library/prefixes/azeroth-survivors
rm -rf "$prefix/drive_c/Games/AzerothSurvivors"
grep -q '"WGPU_ALLOW_UNDERLYING_NONCOMPLIANT_ADAPTER"="1"' "$prefix/user.reg" ||
    { echo "the GPU setting is missing from user.reg" >&2; exit 1; }

python3 - "$prefix/drive_c/windows" "$runtime" <<'PY'
import os, shutil, sys
windows, runtime = sys.argv[1:3]
for folder, arch in (("system32", "x86_64-windows"), ("syswow64", "i386-windows")):
    have = set(os.listdir(os.path.join(runtime, arch)))
    count = 0
    for dirpath, _, files in os.walk(os.path.join(windows, folder)):
        for name in files:
            if name in have:
                shutil.copyfile(os.path.join(runtime, arch, name), os.path.join(dirpath, name))
                count += 1
    print(f"{folder}: {count} modules from the console's runtime")
PY
cp "$here/ps5/profiles/azeroth-survivors.profile" "$library/profiles/azeroth-survivors.profile"

echo "== console layout"
console=$work/console
mkdir -p "$console/data/prospero-win"
python3 - "$console" > "$work/ftp.port" <<'PY' &
import logging, sys
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer
logging.disable(logging.CRITICAL)
auth = DummyAuthorizer(); auth.add_anonymous(sys.argv[1], perm="elradfmwMT")
FTPHandler.authorizer = auth
server = FTPServer(("127.0.0.1", 0), FTPHandler)
print(server.address[1], flush=True)
server.serve_forever()
PY
ftpd=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$work/ftp.port" ] && break; sleep 1; done
python3 "$work/pw/tools/pw_prefix.py" push azeroth-survivors --library "$library" \
    --host 127.0.0.1 --port "$(cat "$work/ftp.port")" \
    --cpu-dll "$runtime/x86_64-windows/wowprospero.dll"
kill "$ftpd"

echo "== package"
name=azeroth-survivors-prefix-$tag.zip
(cd "$console/data/prospero-win/prefixes/azeroth-survivors" && zip -q -r -X "$out/$name" .)
python3 - "$out/$name" "$tag" "$("$wine" --version)" > "$out/prefix.json" <<'PY'
import hashlib, json, sys, zipfile
path, tag, wine = sys.argv[1:4]
digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
with zipfile.ZipFile(path) as z:
    files = sum(1 for i in z.infolist() if not i.is_dir())
    unpacked = sum(i.file_size for i in z.infolist())
import os
print(json.dumps({"prospero_win": tag, "wine": wine, "asset": os.path.basename(path),
                  "size": os.path.getsize(path), "sha256": digest,
                  "files": files, "unpacked": unpacked}, indent=1))
PY
cat "$out/prefix.json"
