# Azeroth Survivors on PS5

Play **[Azeroth Survivors](https://wow.aethus.pl/)**, the free fan-made WoW
roguelike by Aethus, on a jailbroken PS5. It uses
**[prospero-win](https://github.com/mpereiraesaa/prospero-win)**, which runs
Windows games on the console with its own copy of Wine.

**Status: it runs on a PS5** with prospero-win v0.2.0 (tested 2026-10-26):
smooth, full screen, DualSense through the game's own gamepad support.

This repository has no game files in it. It has:

- a **launcher profile** and a **DualSense preset** for prospero-win
  (`ps5/`);
- the **Wine prefix** the game runs in (its C: drive and registry), built by
  GitHub Actions from prospero-win's own release and published under
  [Releases](../../releases). It holds no game files;
- **`azs_ps5.py`**, one Python script that puts all of that and the game on
  the PS5 over FTP, adds it to the launcher and keeps it up to date from the
  game's own official update server.

If it doesn't work for you, please [open an issue](../../issues) with your
firmware, your prospero-win version and the newest
`/data/prospero-win/logs/session-*.log`.

## How it runs

| | |
|---|---|
| Program | `AzerothSurvivors.exe`, 64-bit (Rust + Bevy). It runs natively on the PS5's CPU, so there's no x86 translation. |
| Graphics | wgpu → Vulkan → winevulkan → the console's Vulkan driver (RADV). No DXVK, no Zink. wgpu turns the console's driver down as "not Vulkan compliant" unless `WGPU_ALLOW_UNDERLYING_NONCOMPLIANT_ADAPTER=1` is set (the game then stops with *"Unable to find a GPU!"*), so the prefix's registry sets that and `WGPU_BACKEND=vulkan`. |
| Wine prefix | prospero-win can't create one on the console (it can't run `wineboot` there), so the game gets a prefix of its own, made on a PC with the same Wine version and with every Windows module replaced by the copy in prospero-win's release. |
| Controller | The game has its own gamepad support. Under Wine it reads controllers through XInput, and prospero-win makes the DualSense look like an Xbox controller, so the game sees it as controller 0. |
| Keyboard and mouse | USB keyboard and mouse work alongside the pad. The mouse wheel doesn't work in prospero-win yet, so camera zoom by wheel is out. |
| Updates | The in-game updater runs on the console, and `azs_ps5.py update` does the same job from your PC. See [Updates](#updates). |

The details, and how each one was checked, are in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## What you need

**On the PS5**

- A jailbreak that runs homebrew, with an FTP server on port 2121 and an
  ELF loader (for example `ftpsrv` and `elfldr` from
  [ps5-payload-dev](https://github.com/ps5-payload-dev)).
- **prospero-win v0.2.0**, installed and opened once. The Wine prefix is
  made for that version's runtime. Follow its
  [Getting started](https://github.com/mpereiraesaa/prospero-win/blob/main/docs/GETTING_STARTED.md)
  steps 1 and 2, which use the zip from its
  [Releases](https://github.com/mpereiraesaa/prospero-win/releases) and a
  loader such as ShadowMountPlus.

**On your PC** (Windows, macOS or Linux)

- **Python 3.8 or newer.** Nothing else needs installing.
- Something that can open `.rar` files. Windows 10/11 and macOS already
  have it (their built-in `tar`). On Linux, install `bsdtar`, `unrar`,
  `7z` or `unar`.
- About 5 GB of free space while installing.

## Install

```sh
git clone https://github.com/ebreen/azeroth-survivors-ps5
cd azeroth-survivors-ps5

# 1. Get the Windows build (~725 MB) from the developers' download mirror.
python3 azs_ps5.py fetch

# 2. Copy it to the PS5: the Wine prefix (~150 MB download), the game, its
#    launcher profile and controller preset, then update it to the latest version.
python3 azs_ps5.py install AzerothSurvivorsWindowsNew.rar --host 192.168.1.50
```

Use your PS5's IP address. On Windows, type `py` instead of `python3`.

`fetch` downloads from the developers' mirror,
[azerothsurvivors-mirror.sandervspl.dev](https://azerothsurvivors-mirror.sandervspl.dev/).
It reads the current file and its SHA-256 from the mirror's page, checks the
download against that SHA-256, and carries on from where it stopped if it was
interrupted. The official link on wow.aethus.pl is much slower (around
70 KB/s in our test, against 20–30 MB/s from the mirror). `fetch` uses it only
when the mirror is down or its file fails the check; `--official` forces it.
If you already have the `.rar`, skip `fetch` and give `install` its path.

The first copy is about 2.1 GB (the prefix is ~1,800 small files, which the
PS5's FTP server is slowest at). Expect 3–6 minutes. If it gets interrupted,
run the same command again: files already on the console are skipped.

Then start **prospero-win** on the PS5 and pick **Azeroth Survivors**.

### Files that go on the console

```text
/data/prospero-win/
  prefixes/azeroth-survivors/                              the game's Wine prefix (C: drive, registry)
  prefixes/azeroth-survivors/drive_c/Games/AzerothSurvivors/   AzerothSurvivors.exe, Data/*.MPQ
  profiles/azeroth-survivors.profile
  profiles/profiles.lst                    the game is added if the list exists
  input/azeroth-survivors.input
```

You don't need a Linux PC or a Wine build: the prefix comes ready-made from
this repository's releases (`ps5/prefix.json` pins its SHA-256). An existing
prefix on the console is kept, so reinstalling or updating doesn't reset the
game's settings; `--reinstall-prefix` sends it again. If you'd rather copy
things yourself (another FTP client, a USB drive), `--to DIR` writes the same
layout into a local folder:

```sh
python3 azs_ps5.py install AzerothSurvivorsWindowsNew.rar --to ./ps5-copy
```

## Controls

The DualSense acts as an Xbox controller and the game uses its own gamepad
bindings. You can change them in the game's options, under **Gamepad**.

| DualSense | Xbox |
|---|---|
| Cross / Circle / Square / Triangle | A / B / X / Y |
| L1 / R1, L2 / R2 | LB / RB, LT / RT |
| Options | Start |
| Create, or a click on the touchpad's left half | Back |
| Sticks, d-pad | Sticks, d-pad |

**To quit**, hold **Options + Create** for 3 seconds. That takes you back to
prospero-win's launcher.

A USB keyboard and mouse work at the same time. The game's keys are WASD to
move, F for the movement spell, Space to jump and Esc for the menu; the full
list is in the game's options.

## Updates

**From your PC (recommended):**

```sh
python3 azs_ps5.py status --host 192.168.1.50   # is there a new version?
python3 azs_ps5.py update --host 192.168.1.50   # install it
```

This reads the same update server the game uses (`wow.aethus.pl/exe/latest.json`
and `.../data/patches.json`). It downloads the new program and any new or
changed game data, checks every SHA-256 and sends only what changed.

**On the console, from the game:** when a new version is out, the game offers
**Download & restart**. The download and install work on the PS5. The
restart can't: a PS5 app runs one process and can't start a new one. So
after installing, the game shows *"Could not put the update in place"*.
That message comes from the restart step only. The new files are already
in place. Hold **Options + Create** to quit, start the game again from the
launcher, and you're on the new version.

If you'd rather not see that message, choose **Later** in the game and run
`update` from your PC instead.

## Other commands

```sh
python3 azs_ps5.py status    --host IP [--offline]   # what's installed
python3 azs_ps5.py uninstall --host IP               # remove the game, its prefix, profile and preset
```

Each command also takes `--port` (default 2121), `--remote` (default
`/data/prospero-win`) and `--to DIR` in place of `--host`.

## Troubleshooting

- **It doesn't show in the launcher.** If you have a
  `/data/prospero-win/profiles/profiles.lst`, the launcher shows only what's
  listed there, up to 16 games. `install` adds the game unless the list is
  already full, in which case it tells you.
- **It goes straight back to the launcher.** Look at the newest
  `/data/prospero-win/logs/session-*.log`:
  - `could not load kernel32.dll`: the Wine prefix is missing or incomplete.
    Run `install` again (or with `--reinstall-prefix`).
  - `Unable to find a GPU!`: the prefix's registry lacks the GPU settings.
    Run `install` again; it puts them back without touching anything else.
  - anything else: open an issue and attach the log (prospero-win's
    [debugging guide](https://github.com/mpereiraesaa/prospero-win/blob/main/docs/DEBUGGING_GUIDE.md)
    explains them).
- **A newer prospero-win than v0.2.0.** The prefix's Windows modules come from
  v0.2.0's runtime. If a newer prospero-win won't start the game, open an
  issue; a prefix for it is one workflow run (`.github/workflows/prefix.yml`).
- **The pad does nothing.** The profile's preset has to be on the console
  (`input/azeroth-survivors.input`). Run `install` again, or `status`, which
  reports whether it's there.
- **The picture fills only part of the screen.** In the game's graphics
  options, pick *Windowed (Fullscreen)*. prospero-win scales the game's
  window to the TV either way (`view = window`, `scaling = fit`).

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests run `azs_ps5.py` against a local folder, an FTP server (if
`pyftpdlib` is installed), a fake update server and a fake prefix release. CI
also checks the
profile and preset with
[prospero-win-profiles](https://github.com/mpereiraesaa/prospero-win-profiles)'
`check_profiles.py`, which reads them the same way prospero-win does.

`tools/build_prefix.sh` builds the prefix (on x86-64 Linux with WineHQ's
wine-devel 11.17); the **Wine prefix** workflow runs it and publishes the
release. `recipes/azeroth-survivors.yml` is the same setup as a
[prospero-win](https://github.com/mpereiraesaa/prospero-win) `pw_install.py`
recipe, for installing it the way prospero-win's own docs describe.

## Legal

*Azeroth Survivors* is a free fan game by **Aethus**, made with World of
Warcraft art from a WoW 1.12.1 client. It is not affiliated with or endorsed
by Blizzard Entertainment. This repository ships no game files, no Blizzard
content and no Windows programs of its own; the prefix release contains Wine's
modules (LGPL-2.1-or-later; sources are linked from the release). The script
downloads the game only from the
developers' download mirror, Aethus's own site and the game's own update
server.
World of Warcraft is a trademark of Blizzard Entertainment, Inc.
prospero-win is LGPL-2.1-or-later, by its authors.

The code here is MIT-licensed ([LICENSE](LICENSE)).
