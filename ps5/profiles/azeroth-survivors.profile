; Azeroth Survivors (the free fan-made WoW roguelike by Aethus,
; https://wow.aethus.pl/), Windows build, on prospero-win.
;
; Installed by azs_ps5.py from github.com/ebreen/azeroth-survivors-ps5:
; the game folder (AzerothSurvivors.exe and Data\) goes to
; C:\Games\AzerothSurvivors of the default prefix
; (/data/prospero-win/prefix/drive_c/Games/AzerothSurvivors).
;
; - 64-bit (PE64) Rust/Bevy program: runs natively on the console's CPU, no
;   translator.
; - Graphics: wgpu's Vulkan backend straight through winevulkan to the
;   console's Vulkan driver. No DXVK and no Zink are involved, so graphics
;   stays "auto". d3d12 and d3d12core are disabled so wgpu cannot pick its
;   Direct3D 12 backend (vkd3d) over Vulkan; the console's Wine has no host
;   OpenGL, so the GL backend never comes up either.
; - Controller: the game reads gamepads through XInput when it runs under
;   Wine (it logs "pad: running under Wine, gamepads through XInput"), so the
;   DualSense is its Xbox controller 0. USB keyboard and mouse work too.
; - In-game updates download and install on the console. The game then
;   tries to restart itself, which a PS5 title cannot do (one process), so it
;   reports "Could not put the update in place"; quit (hold Options + Create)
;   and start it again from the launcher to play the new version.
[application]
id = azeroth-survivors
name = Azeroth Survivors
executable = C:\Games\AzerothSurvivors\AzerothSurvivors.exe
working_directory = C:\Games\AzerothSurvivors
dll_overrides = d3d12,d3d12core=d
prefix = azeroth-survivors
runtime = wine-wow64
architecture = pe64
graphics = auto

[display]
desktop = 1920x1080
scaling = fit
view = window
show_fps = false

[input]
preset = azeroth-survivors
