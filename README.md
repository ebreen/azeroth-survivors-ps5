# Azeroth Survivors on PS5

Play **[Azeroth Survivors](https://wow.aethus.pl/)**, the free fan-made WoW
roguelike by Aethus, on a jailbroken PS5. It uses
**[prospero-win](https://github.com/mpereiraesaa/prospero-win)**, which runs
Windows games on the console with its own copy of Wine.

This repository has no game files in it. It has:

- a **launcher profile** and a **DualSense preset** for prospero-win
  (`ps5/`);
- **`azs_ps5.py`**, one Python script that copies the game to the PS5 over
  FTP, adds it to the launcher and keeps it up to date from the game's own
  official update server.

> [!WARNING]
> Not tested on a PS5 yet. The setup follows how prospero-win runs games,
> and the profile and preset pass prospero-win's own parser. The game's
> renderer, gamepad code and updater were checked by reading the Windows
> build, not by playing it on a console. If you try it, please
> [open an issue](../../issues) with your firmware, your prospero-win
> version and what happened. A log helps a lot.

## How it runs

| | |
|---|---|
| Program | `AzerothSurvivors.exe`, 64-bit (Rust + Bevy). It runs natively on the PS5's CPU, so there's no x86 translation. |
| Graphics | wgpu → Vulkan → winevulkan → the console's Vulkan driver (RADV). No DXVK, no Zink. The profile turns off `d3d12` so wgpu can't pick its Direct3D 12 path. |
| Controller | The game has its own gamepad support. Under Wine it reads controllers through XInput, and prospero-win makes the DualSense look like an Xbox controller, so the game sees it as controller 0. |
| Keyboard and mouse | USB keyboard and mouse work alongside the pad. The mouse wheel doesn't work in prospero-win yet, so camera zoom by wheel is out. |
| Updates | The in-game updater runs on the console, and `azs_ps5.py update` does the same job from your PC. See [Updates](#updates). |

The details, and how each one was checked, are in [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## What you need

**On the PS5**

- A jailbreak that runs homebrew, with an FTP server on port 2121 and an
  ELF loader (for example `ftpsrv` and `elfldr` from
  [ps5-payload-dev](https://github.com/ps5-payload-dev)).
- **prospero-win**, installed and opened once. Follow its
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

# 1. Get the official Windows build (~725 MB) from wow.aethus.pl.
#    Or download it yourself from the site's "download the game for Windows" link.
python3 azs_ps5.py fetch

# 2. Copy it to the PS5: the game, its launcher profile and controller preset,
#    then update it to the latest version.
python3 azs_ps5.py install AzerothSurvivorsWindowsNew.rar --host 192.168.1.50
```

Use your PS5's IP address. On Windows, type `py` instead of `python3`.

The first copy is about 1.6 GB. The PS5's FTP server manages about
15–20 MB/s, so it takes a couple of minutes. If it gets interrupted, run
the same command again: files already on the console are skipped.

Then start **prospero-win** on the PS5 and pick **Azeroth Survivors**.

### Files that go on the console

```text
/data/prospero-win/
  prefix/drive_c/Games/AzerothSurvivors/   AzerothSurvivors.exe, Data/*.MPQ   (C:\Games\AzerothSurvivors)
  profiles/azeroth-survivors.profile
  profiles/profiles.lst                    the game is added if the list exists
  input/azeroth-survivors.input
```

The game goes into prospero-win's **default prefix**, so you don't need a
Linux PC or a Wine build. If you'd rather copy things yourself (another FTP
client, a USB drive), `--to DIR` writes the same layout into a local folder:

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
python3 azs_ps5.py uninstall --host IP               # remove the game, profile and preset
```

Each command also takes `--port` (default 2121), `--remote` (default
`/data/prospero-win`) and `--to DIR` in place of `--host`.

## Troubleshooting

- **It doesn't show in the launcher.** If you have a
  `/data/prospero-win/profiles/profiles.lst`, the launcher shows only what's
  listed there, up to 16 games. `install` adds the game unless the list is
  already full, in which case it tells you.
- **Black screen or it goes straight back to the launcher.** Check that your
  prospero-win build runs a 64-bit Vulkan game. Its *Vulkan probe, x64* is
  in its hardware validation, and 64-bit DXVK games like Kerbal Space
  Program run on v0.2.0. Then open an issue and attach the title log (see
  prospero-win's
  [debugging guide](https://github.com/mpereiraesaa/prospero-win/blob/main/docs/DEBUGGING_GUIDE.md)).
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
`pyftpdlib` is installed) and a fake update server. CI also checks the
profile and preset with
[prospero-win-profiles](https://github.com/mpereiraesaa/prospero-win-profiles)'
`check_profiles.py`, which reads them the same way prospero-win does.

If you get it running, a profile for
[prospero-win-profiles](https://github.com/mpereiraesaa/prospero-win-profiles)
would be the next step.

## Legal

*Azeroth Survivors* is a free fan game by **Aethus**, made with World of
Warcraft art from a WoW 1.12.1 client. It is not affiliated with or endorsed
by Blizzard Entertainment. This repository ships no game files, no Blizzard
content and no Windows programs. The script downloads the game only from
Aethus's own site and update server, the same places the game itself uses.
World of Warcraft is a trademark of Blizzard Entertainment, Inc.
prospero-win is LGPL-2.1-or-later, by its authors.

The code here is MIT-licensed ([LICENSE](LICENSE)).
