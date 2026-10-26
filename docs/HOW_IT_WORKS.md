# How it works, and what was checked

These notes come from reading the official Windows build
(`AzerothSurvivors.exe`: v0.115.0 from `AzerothSurvivorsWindowsNew.rar`,
and v0.120.0 from the update server) together with prospero-win's source. They
haven't been checked on a console yet.

## The program

- PE32+ x86-64, built with Rust, Bevy 0.18 and wgpu 27. wgpu-hal includes
  its Vulkan, DX12 and GLES backends.
- `opengl32.dll` is a static import, while `vulkan-1.dll` and `d3d12.dll`
  are loaded at run time.
- The game looks for its data in `Data\` or `WoW\Data\` beside the exe, or in
  `WOW_DATA`. The profile's working folder is the game folder, so it finds
  `Data\` there.

## Graphics on the console

prospero-win's Wine has Vulkan (winevulkan → `libvulkan.prx`, RADV) but no
host OpenGL (`--without-opengl`). Its *Vulkan probe, x64* creates an
instance, device, win32 surface and swapchain on the console, which is the
same path wgpu's Vulkan backend takes.

wgpu chooses its adapter by device type, so a Direct3D 12 adapter that
Wine's vkd3d reports as "discrete" could win over RADV's integrated Vulkan
device. To rule that out, the profile disables `d3d12` and `d3d12core`
(`dll_overrides = d3d12,d3d12core=d`; any letter other than `n` or `b`
disables a DLL). The GL backend can't start without host OpenGL, so Vulkan
is the only backend left.

prospero-win's `graphics` setting only chooses DXVK's or Zink's environment
and HUD, and this game uses neither, so the profile says `graphics = auto`
and `show_fps = false`.

## Gamepad

`crates\wenilla-rogue-app\src\screens\xinput.rs` checks for Wine
(`ntdll!wine_get_version`). It then loads `xinput1_4.dll`, `xinput1_3.dll`
or `xinput9_1_0.dll` and polls `XInputGetState`, logging *"pad: running under
Wine, gamepads through XInput"*. On the console, prospero-win's Wine patch
0470 connects every xinput DLL built from xinput1_3's source, including
xinput1_4, to the title's DualSense when the profile's input mode is
`xinput`. The preset sets `mode = xinput` and `mouse = none`.

## The in-game updater

`crates\wenilla-rogue-app\src\update.rs`, as disassembled:

1. It reads `https://wow.aethus.pl/exe/latest.json` (`WOW_ROGUE_UPDATE_URL`)
   and `.../exe/data/patches.json` (`WOW_ROGUE_DATA_URL`). Each item has a
   size, a SHA-256 and a list of parts. Joined together, the parts are a
   gzip stream of the file. `azs_ps5.py` reads the same lists and verified
   v0.120.0's exe this way (SHA-256 `291c281b…`).
2. HTTPS goes through ureq 2 and rustls 0.23 with a built-in root store, so
   the console's Wine schannel isn't involved. Sockets and DNS go through
   prospero-win's `ws2_32.prx` and the console's resolver.
3. Downloads go to `.update\` beside the exe. Applying the update moves the
   new `Data\` files into place, then renames the running exe to `*.old` and
   puts the new one in its place.
4. **Last of all** it starts the new exe (`std::process::Command::spawn`)
   and exits. If that spawn fails, the error is returned and the screen
   shows *"Could not put the update in place: {error}"*.

A PS5 title can't create processes (prospero-win's Wine patch 0550), so
step 4 always fails on the console. Steps 1–3 have already run by then,
though, so quitting and starting the game again from the launcher runs the
new version. The README tells players to do that.

`azs_ps5.py update` does steps 1–3 from a PC. It doesn't need the restart,
and it checks the PS5's exe by size and SHA-256, so it notices when the game
has already updated itself.
