<br />
<div align="center">
  <a href="https://github.com/Neyroe/External-Brawl-Camera/">
    <img width="400" alt="external-brawl-camera-logo" src="https://github.com/user-attachments/assets/d3dba29a-f0a7-4765-96fc-1df3ab76c649" />
  </a>
<h1 align="center">External Brawl Camera</h1>
</div>

## DESCRIPTION
External Brawl Camera (EBC) is a Blender extension that drives the camera of Super Smash Bros. Brawl / Project+ running in Dolphin, in real time, by reading and writing the game's memory.

It started as a port of External Melee Camera made by KELLZ: https://github.com/sadkellz/External-Melee-Camera

## INSTALLATION
**One-shot install** (finds or downloads Blender 5.2 LTS, installs EBC, fetches the stages scene, checks Dolphin):

```bash
# Linux
git clone https://github.com/Neyroe/External-Brawl-Camera.git
cd External-Brawl-Camera && tools/install/install.sh
```
```powershell
# Windows 11 (no admin rights needed)
git clone https://github.com/Neyroe/External-Brawl-Camera.git
cd External-Brawl-Camera; powershell -ExecutionPolicy Bypass -File tools\install\install.ps1
```
Options: `--help` / `-Help`. More in [tools/install/README.md](tools/install/README.md).

**Manual install**: download `external_brawl_camera-X.Y.Z.zip` from the [latest release](https://github.com/Neyroe/External-Brawl-Camera/releases/latest), then in Blender: Edit > Preferences > Get Extensions > Install from Disk.

Nothing to install with pip anymore: the memory library is bundled. Coming from V2? Remove the old add-on and the hand-installed `dolphin_memory_engine` (the installer offers to do it).

**Use**: 3D viewport > sidebar (N) > **EBC** tab > **Connect** > **Start sync**.

## IMPORTANT
>Made for **Project+ 3.1.5 and 3.2** (detected automatically) on Dolphin, with **Blender 4.2+** (tested on 5.2 LTS and 4.3).

- One Dolphin at a time per Blender session.
- Linux: `kernel.yama.ptrace_scope` must be 0 (`sudo sysctl kernel.yama.ptrace_scope=0`), and Blender must be a native install (not Flatpak/Snap).
- EBC finds the Dolphin process itself (including the Project+ 3.2 AppImage). If you set `DME_DOLPHIN_PROCESS_NAME`, it must match the running Dolphin.
- Pause, frame advance and savestates from Blender need a Dolphin build with the DevServer; everything else works on any Dolphin, including the Project+ 3.2 AppImage.
- Project M 3.6 / 3.6R: not tested.

## FEATURES
**Camera**
1. Blender camera drives the Brawl camera, or the reverse — exact to the pixel, no lag, no shake
2. Native camera freeze: no Code Menu needed, survives pause, hand back to the game camera at any frame
3. FOV, roll, near/far clip, screen shake removal
4. new ! Orthographic and one-click **Isometric** / **Dimetric 2:1** views
5. new ! Multiple cameras: one click to cut, or switch on timeline markers
6. new ! **Record** the game's own camera frame by frame into Blender, and blend between it and yours

**Scene & timeline**
1. Player positions in the scene, one-click rig creation
2. Blender timeline follows the game frame (or starts at a chosen frame)
3. new ! Pause, next frame, save/load state from Blender (DevServer)

**Green screen**
1. Full-screen background colour with a working alpha (veil over the stage → flat key colour)
2. Void colour, stage hidden / collision only
3. new ! Per-character: each fighter **Normal**, **Colour** (flat tint) or **Hidden**, and **Isolate** one fighter on a flat background in one click *(experimental)*

**Game display**
1. HUD, debug menu, Draw DI, hurtboxes, character and stage visibility
2. Shadow light angles, music and sound effect volume

Every setting is re-applied on a new match, and disconnecting restores everything EBC changed.

Could be added in near futur:
1. DevServer for the Project+ 3.2 Dolphin and Windows
2. Screenshot, image & preview sequence from Blender

## CONTACT & SUPPORT
You are free to ask me anything on discord at :
>Neyroe#4096

# DOCUMENTATION
## EXAMPLES
### EXAMPLE 1
https://github.com/Neyroe/External-Brawl-Camera/assets/62217068/aa17afc3-4aed-4bb3-9fb1-44990827671d
### EXAMPLE 2
https://github.com/Neyroe/External-Brawl-Camera/assets/62217068/2ca67b02-f303-4c83-93e9-77ab19153c30
### EXAMPLE 3
https://github.com/Neyroe/External-Brawl-Camera/assets/62217068/d5152493-a1f6-4875-99b4-344b16e9bb68
### EXAMPLE 4
https://github.com/Neyroe/External-Brawl-Camera/assets/62217068/259a4cec-7c33-430e-ba9c-e5cbc13d1502

## Optionnal
- **Stages scene**: the `ebc_stages` Blender scene (stages already modelled) is attached to the [releases](https://github.com/Neyroe/External-Brawl-Camera/releases).
- **Match game aspect** (Camera panel) sets your render resolution to the game's aspect, so Blender frames exactly what Brawl shows.
- **Center blender camera**: the "3D View: 3D Navigation" add-on can be helpful.

## Known Issues
- **Orthographic on Pokémon Stadium 2**: the centre floor panel is not drawn (Final Destination is fine).
- **Per-character colour**: effects (projectiles, Din's Fire…) are neither tinted nor hidden; Ice Climbers and Pokémon Trainer untested.
- The orientation of a camera whose view never meets the stage plane cannot be represented exactly (a warning shows).
- **Windows installer**: tested with PowerShell 7 only, never on a real Windows yet.

# Acknowledgments
[External Melee Camera](https://github.com/sadkellz/External-Melee-Camera)
- For revolutionnasing the whole content creation scene !!

Thanks to [KELLZ](https://github.com/sadkellz)
- For his amazing work and help.

Thanks to [WhiteTpoison](https://github.com/JaredWhiteOne)
- For helping on **EBC** & his work on **BrawlBack**.

Thanks to EON
- For the Smash Ball technique that started the EBC green screen.

[ProjectPunch](https://github.com/WispSSBM/ProjectPunch) (Wisp)
- For the per-character colour recipe.

See [LICENSE](LICENSE).
