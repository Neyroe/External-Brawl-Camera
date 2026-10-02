# One-shot installers

`install.sh` (Linux) and `install.ps1` (Windows 10/11, Windows PowerShell 5.1 or PowerShell 7) install External Brawl Camera V3 in one go. No sudo or administrator rights are needed: everything goes into your user profile. Running the script again updates the install.

| Step | What happens |
| --- | --- |
| Blender | Uses a Blender 4.2+ already installed (`--blender`, PATH, usual folders). If there is none, it downloads the latest Blender 5.2 LTS from download.blender.org, checks its SHA-256 and unpacks it into `~/.local/opt/blender-5.2.x` (Linux, plus `~/.local/bin/blender` and a menu entry) or `%LOCALAPPDATA%\Programs\Blender-5.2.x` (Windows, plus a Start menu shortcut). |
| V2 leftovers | Looks for the old `External-Brawl-Camera` add-on and for a `dolphin_memory_engine` installed by hand into Blender's Python. It asks before moving them to the trash (or to a backup folder). Nothing is deleted without asking. |
| Extension | Installs `external_brawl_camera-X.Y.Z.zip` from the latest GitHub release that has one. The "EBC 2.0" release (the old add-on) is never used. If there is no V3 release, it builds the zip from the sources with `blender --command extension build`. The zip bundles dolphin-memory-engine, so nothing is installed with pip. |
| Scene | Downloads an `ebc_stages*.zip` / `.blend` release asset into `Documents/EBC` if one is published. |
| Dolphin | Looks for Project+ / Dolphin and the process names DME will match. On Linux it also checks `kernel.yama.ptrace_scope`. Nothing related to the game is downloaded. |
| Check | Starts Blender headless and checks that the extension is enabled and that `dolphin_memory_engine` imports. |

## Usage

Linux:

```sh
tools/install/install.sh                 # from a checkout
curl -fsSL https://raw.githubusercontent.com/Neyroe/External-Brawl-Camera/project+/tools/install/install.sh | bash
curl -fsSL .../install.sh | bash -s -- --dev   # with options
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File tools\install\install.ps1
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Neyroe/External-Brawl-Camera/project+/tools/install/install.ps1))) -Dev
```

| Linux | Windows | Meaning |
| --- | --- | --- |
| `--dev` | `-Dev` | Developer setup (below). |
| `--blender PATH` | `-Blender PATH` | Use this Blender (4.2 or newer). |
| `--version X.Y.Z` | `-Version X.Y.Z` | Install this EBC version (release asset, else tag `vX.Y.Z`). |
| `--repo-dir DIR` | `-RepoDir DIR` | `--dev`: where to clone (default: the checkout the script runs from, else `~/src/External-Brawl-Camera` / `%USERPROFILE%\source\External-Brawl-Camera`). |
| `--yes` | `-Yes` | Answer yes (removal of the V2 leftovers). |
| `--no-scene` | `-NoScene` | Skip the stages scene. |
| `--dry-run` | `-DryRun` | Show what would be done, change nothing. |

Environment: `EBC_REF` (git ref used to build from source, default `project+`), `EBC_REPO_URL` (clone URL).

## Developer mode

With `--dev` / `-Dev` the script also:

- clones the repository (or uses the checkout it runs from); if the checkout has a `requirements-dev.txt`, also creates `.venv` with Python 3.11+, installs it and pre-commit with its git hook;
- links `ebc/` into the user extension repository as `user_default/ebc`: a symlink on Linux, a directory junction on Windows (no administrator rights or developer mode needed). A zip-installed copy is removed first, so the extension is not loaded twice. Running the script again without `--dev` swaps the link back for the zip.
- makes sure the bundled wheels are extracted. Blender extracts extension wheels into `extensions/.local/lib/pythonX.Y/site-packages` when the add-on is enabled or when it rebuilds its extension cache (`extensions/.cache/compat.dat`). Measured on 4.3.2 and 5.2.2: once that cache is up to date, missing wheels are not extracted again, and the add-on fails with "No module named dolphin_memory_engine". So the script deletes `compat.dat` and enables the add-on headless, which makes Blender sync the wheels itself (the same mechanism as a zip install, so Blender removes them again cleanly). If they are still missing, the script unpacks the platform wheel there. If you delete `extensions/.local` later, run the script again.
