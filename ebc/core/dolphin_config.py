"""Find the Dolphin user folders and bind Dolphin's hotkeys to EBC's DSU pad. No ``bpy``.

Dolphin keeps its ``.ini`` files in a CONFIG folder and its screenshots and
savestates in a DATA folder (``ScreenShots/<GameID>/``, ``StateSaves/``). They
are the same folder (``<user>/Config`` and ``<user>``) with ``-u``,
``$DOLPHIN_EMU_USERPATH``, ``portable.txt`` or ``~/.<name>/``, and two XDG
folders otherwise (``UICommon::SetUserDirectory``):

* P+ 3.2 AppImage: ``$XDG_CONFIG_HOME/project-plus-dolphin`` and
  ``$HOME/.local/share/project-plus-dolphin`` (the AppImage runtime points HOME /
  XDG_CONFIG_HOME to ``*.AppImage.home`` / ``*.AppImage.config`` when they exist);
* standard Dolphin: ``~/.config/dolphin-emu`` and ``~/.local/share/dolphin-emu``
  (Flatpak: under ``~/.var/app/org.DolphinEmu.dolphin-emu/``);
* Windows: ``<folder of Dolphin.exe>\\User`` with ``portable.txt`` (P+ ships it),
  else ``Documents\\Dolphin Emulator``.

On Linux the folders of the RUNNING Dolphin are read from ``/proc/<pid>/cmdline``
and ``/proc/<pid>/environ``; a folder picked in the preferences always wins.

``install()`` writes the one-time configuration (docs/research/screenshot.md 3):
the EBC DSU server in ``DSUClient.ini``, the five EBC hotkeys added to
``Hotkeys.ini`` (keeping the user's keys), ``HotkeysRequireFocus = False`` in
``Dolphin.ini``. Dolphin must be closed: it rewrites ``Dolphin.ini`` and
``DSUClient.ini`` when it exits.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .backend import process_comms
from .dsu import BINDINGS, DEFAULT_PORT, DEVICE, EBC_SLOT, HOST, SERVER_NAME

PPLUS_NAME = "project-plus-dolphin"
DOLPHIN_NAME = "dolphin-emu"
FLATPAK_ID = "org.DolphinEmu.dolphin-emu"
BACKUP_SUFFIX = ".ebc-backup"
NO_FILE_MARKER = "# EBC: this file did not exist before EBC wrote it\n"
# Executables (basename of /proc/<pid>/exe) and comm names of Dolphin builds.
DOLPHIN_EXES = ("dolphin-emu", "dolphin-emu-qt2", "dolphin-emu-nogui", "dolphin-emu-wx", PPLUS_NAME)
DOLPHIN_COMMS = ("dolphin-emu", "dolphin-emu-qt2", "dolphin-emu-nog", "dolphin-emu-wx", "project-plus-do")
WINDOWS_IMAGES = ("Dolphin.exe", "DolphinNoGUI.exe", "Project+ Dolphin.exe")


@dataclass(frozen=True)
class DolphinPaths:
    """Where one Dolphin keeps its configuration (``.ini``) and its data."""

    config: Path
    data: Path
    source: str = ""  # how it was found, for the UI

    def screenshots(self, game_id: str) -> Path:
        return self.data / "ScreenShots" / game_id

    def slot_file(self, game_id: str, slot: int = EBC_SLOT) -> Path:
        return self.data / "StateSaves" / f"{game_id}.s{slot:02d}"


# -- finding the folders ------------------------------------------------------------------


def user_dir_name(exe: str) -> str:
    """Folder name Dolphin uses under the XDG folders: ``project-plus-dolphin`` for P+."""
    base = os.path.basename(exe).lower()
    return PPLUS_NAME if base.startswith("project-plus") or "project+" in base else DOLPHIN_NAME


def _user_dir_from_args(args: Sequence[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg in ("-u", "--user") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--user="):
            return arg.split("=", 1)[1]
        if arg.startswith("-u") and len(arg) > 2:
            return arg[2:]
    return None


def resolve_linux(
    args: Sequence[str],
    env: Mapping[str, str],
    exe: str,
    cwd: str | None = None,
    exists=os.path.exists,
) -> DolphinPaths | None:
    """Apply ``UICommon::SetUserDirectory`` (Linux) to a process's arguments and environment."""
    name = user_dir_name(exe)
    user = _user_dir_from_args(args[1:])
    if user:
        user_path = Path(user if os.path.isabs(user) or not cwd else os.path.join(cwd, user))
        return DolphinPaths(user_path / "Config", user_path, "-u argument")
    if cwd and exists(os.path.join(cwd, "user")):  # ROOT_DIR/EMBEDDED_USER_DIR: "./user"
        user_path = Path(cwd) / "user"
        return DolphinPaths(user_path / "Config", user_path, "user folder in the working directory")
    exe_dir = os.path.dirname(exe)
    if exe_dir and exists(os.path.join(exe_dir, "portable.txt")):
        user_path = Path(exe_dir) / "user"
        return DolphinPaths(user_path / "Config", user_path, "portable.txt")
    if env.get("DOLPHIN_EMU_USERPATH"):
        user_path = Path(env["DOLPHIN_EMU_USERPATH"])
        return DolphinPaths(user_path / "Config", user_path, "DOLPHIN_EMU_USERPATH")
    home = env.get("HOME") or env.get("PWD") or ""
    legacy = Path(home) / f".{name}"
    if not env.get("FLATPAK_ID") and exists(str(legacy)):
        return DolphinPaths(legacy / "Config", legacy, f"~/.{name}")
    if not home:
        return None
    config_home = env.get("XDG_CONFIG_HOME", "")
    data_home = env.get("XDG_DATA_HOME", "")
    config_home = config_home if config_home.startswith("/") else os.path.join(home, ".config")
    data_home = data_home if data_home.startswith("/") else os.path.join(home, ".local", "share")
    return DolphinPaths(Path(config_home) / name, Path(data_home) / name, "XDG folders")


def resolve_windows(exe: str, env: Mapping[str, str], exists=os.path.exists) -> DolphinPaths:
    """``SetUserDirectory`` on Windows (registry keys left out): ``portable.txt`` next to the
    exe -> ``<exe folder>\\User``; else ``Documents\\Dolphin Emulator`` if it exists; else
    ``AppData\\Roaming\\Dolphin Emulator``. Not tested on Windows."""
    exe_dir = os.path.dirname(exe)
    if exe_dir and exists(os.path.join(exe_dir, "portable.txt")):
        user = Path(exe_dir) / "User"
        return DolphinPaths(user / "Config", user, "portable.txt next to Dolphin.exe")
    profile = env.get("USERPROFILE", os.path.expanduser("~"))
    docs = Path(profile) / "Documents" / "Dolphin Emulator"
    if exists(str(docs)):
        return DolphinPaths(docs / "Config", docs, "Documents\\Dolphin Emulator")
    appdata = Path(env.get("APPDATA") or os.path.join(profile, "AppData", "Roaming")) / "Dolphin Emulator"
    return DolphinPaths(appdata / "Config", appdata, "AppData\\Roaming\\Dolphin Emulator")


def windows_dolphin_exes() -> list[str]:
    """Paths of the running Dolphin executables (Windows, PowerShell). Not tested on Windows."""
    names = ",".join(os.path.splitext(n)[0].replace(" ", "?") for n in WINDOWS_IMAGES)
    cmd = f"Get-Process -Name {names} -ErrorAction SilentlyContinue | ForEach-Object {{ $_.Path }}"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _read_proc(proc: str, pid: int, what: str) -> bytes | None:
    try:
        with open(f"{proc}/{pid}/{what}", "rb") as f:
            return f.read()
    except OSError:
        return None


def process_paths(pid: int, proc: str = "/proc") -> DolphinPaths | None:
    """User folders of the running Dolphin ``pid`` (Linux, same user)."""
    cmdline = _read_proc(proc, pid, "cmdline")
    environ = _read_proc(proc, pid, "environ")
    if cmdline is None or environ is None:
        return None
    args = [a.decode(errors="replace") for a in cmdline.split(b"\0") if a]
    env = {}
    for item in environ.split(b"\0"):
        key, sep, value = item.decode(errors="replace").partition("=")
        if sep:
            env[key] = value
    try:
        exe = os.readlink(f"{proc}/{pid}/exe")
    except OSError:
        exe = args[0] if args else ""
    try:
        cwd = os.readlink(f"{proc}/{pid}/cwd")
    except OSError:
        cwd = None
    paths = resolve_linux(args, env, exe, cwd)
    if paths is None:
        return None
    return DolphinPaths(paths.config, paths.data, f"running Dolphin (pid {pid}, {paths.source})")


def dolphin_pids(names: Sequence[str] = (), proc: str = "/proc") -> list[int]:
    """Dolphin processes (Linux): comm in ``names`` or a known Dolphin, or a Dolphin executable."""
    wanted = set(names) | set(DOLPHIN_COMMS)
    pids = []
    for pid, comm in (process_comms(proc) or {}).items():
        hit = comm in wanted
        if not hit:
            try:
                hit = os.path.basename(os.readlink(f"{proc}/{pid}/exe")) in DOLPHIN_EXES
            except OSError:
                hit = False
        if hit:
            pids.append(pid)
    return sorted(pids)


def find_running(names: Sequence[str] = (), proc: str = "/proc") -> DolphinPaths | None:
    """Folders of the Dolphin DME hooks: the first process named in ``names`` (DME's choice)."""
    for pid in dolphin_pids(names, proc):
        comm = (_read_proc(proc, pid, "comm") or b"").decode(errors="replace").strip()
        if names and comm not in names:
            continue
        paths = process_paths(pid, proc)
        if paths is not None:
            return paths
    return None


def find_running_dolphin(names: Sequence[str] = (), platform: str = sys.platform) -> DolphinPaths | None:
    """Folders of the running Dolphin (the one DME hooks), None when unknown."""
    if platform.startswith("win"):
        exes = windows_dolphin_exes()
        if not exes:
            return None
        paths = resolve_windows(exes[0], os.environ)
        return DolphinPaths(paths.config, paths.data, f"running Dolphin ({paths.source})")
    if os.path.isdir("/proc"):
        return find_running(names)
    return None


def paths_from_folder(folder: str | os.PathLike, home: str | None = None) -> DolphinPaths | None:
    """Folders from what the user picked: a Dolphin user folder (``Config/`` inside), an XDG
    config folder (``Dolphin.ini`` inside), or the folder of ``Dolphin.exe`` (``User/`` inside)."""
    base = Path(os.path.expanduser(str(folder))).resolve() if str(folder) else None
    if base is None or not base.is_dir():
        return None
    for user in ("User", "user"):
        if (base / user).is_dir() and not (base / "Config").is_dir():
            return DolphinPaths(base / user / "Config", base / user, "Dolphin folder (preferences)")
    if (base / "Config").is_dir() or (base / "StateSaves").is_dir() or (base / "ScreenShots").is_dir():
        return DolphinPaths(base / "Config", base, "Dolphin folder (preferences)")
    if (base / "Dolphin.ini").exists() or base.name in (PPLUS_NAME, DOLPHIN_NAME):
        return DolphinPaths(base, xdg_data_for_config(base, home), "Dolphin folder (preferences)")
    return DolphinPaths(base / "Config", base, "Dolphin folder (preferences)")


def xdg_data_for_config(config: Path, home: str | None = None) -> Path:
    """The data folder that goes with an XDG config folder ``<config home>/<name>``."""
    name = config.name
    parent = config.parent
    if parent.name.endswith(".AppImage.config"):  # portable AppImage: the .home next to it
        app_home = parent.with_name(parent.name[: -len(".config")] + ".home")
        return app_home / ".local" / "share" / name
    if parent.name == "config" and parent.parent.parent.name == "app":  # Flatpak ~/.var/app/<id>/config
        return parent.parent / "data" / name
    if parent.name == ".config":
        return parent.parent / ".local" / "share" / name
    home = home or os.path.expanduser("~")
    return Path(home) / ".local" / "share" / name


def default_candidates(platform: str = sys.platform, home: str | None = None) -> list[DolphinPaths]:
    """Usual places when no Dolphin runs and nothing was picked; only those that exist."""
    home = home or os.path.expanduser("~")
    found = []
    if platform.startswith("win"):
        appdata = Path(os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"))
        for base, label in ((Path(home) / "Documents", "Documents"), (appdata, "AppData\\Roaming")):
            user = base / "Dolphin Emulator"
            if (user / "Config").is_dir():
                found.append(DolphinPaths(user / "Config", user, f"{label}\\Dolphin Emulator"))
        return found
    if platform == "darwin":
        lib = Path(home) / "Library" / "Application Support"
        for name in ("Dolphin", "Project+ Dolphin"):
            if (lib / name / "Config").is_dir():
                found.append(DolphinPaths(lib / name / "Config", lib / name, f"~/Library/Application Support/{name}"))
        return found
    for name in (PPLUS_NAME, DOLPHIN_NAME):
        legacy = Path(home) / f".{name}"
        if legacy.is_dir():
            found.append(DolphinPaths(legacy / "Config", legacy, f"~/.{name}"))
        config = Path(home) / ".config" / name
        if config.is_dir():
            found.append(DolphinPaths(config, Path(home) / ".local" / "share" / name, f"~/.config/{name}"))
    flatpak = Path(home) / ".var" / "app" / FLATPAK_ID
    if (flatpak / "config" / DOLPHIN_NAME).is_dir():
        found.append(
            DolphinPaths(flatpak / "config" / DOLPHIN_NAME, flatpak / "data" / DOLPHIN_NAME, "Flatpak Dolphin")
        )
    return found


def locate(
    manual: str = "",
    detected: DolphinPaths | None = None,
    platform: str = sys.platform,
    home: str | None = None,
) -> tuple[DolphinPaths | None, str]:
    """The folders to use: the one picked in the preferences, else the running Dolphin's
    (``detected``), else the only usual one that exists. Returns (paths, message)."""
    if manual:
        paths = paths_from_folder(manual, home)
        if paths is None:
            return None, f"Dolphin folder {manual!r} does not exist"
        return paths, paths.source
    if detected is not None:
        return detected, detected.source
    found = default_candidates(platform, home)
    if len(found) == 1:
        return found[0], found[0].source
    if found:
        listed = ", ".join(str(p.config) for p in found)
        return None, f"Several Dolphin configurations ({listed}): connect once with Dolphin running, or pick one"
    return None, "Dolphin folder unknown: connect once with Dolphin running, or pick it in the preferences"


def running_dolphins(platform: str = sys.platform, proc: str = "/proc", names: Sequence[str] = ()) -> list[str]:
    """Running Dolphins, as text, with their config folder on Linux (empty list = none)."""
    if platform.startswith("win"):
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [
            line.split(",")[0].strip('"')
            for line in out.splitlines()
            if line.split(",")[0].strip('"') in WINDOWS_IMAGES
        ]
    if not os.path.isdir(proc):
        try:
            out = subprocess.run(["pgrep", "-l", "-i", "dolphin"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in out.splitlines() if line.strip()]
    found = []
    for pid in dolphin_pids(names, proc):
        paths = process_paths(pid, proc)
        found.append(f"pid {pid}" + (f" ({paths.config})" if paths else ""))
    return found


def dolphin_uses(
    config: Path, platform: str = sys.platform, proc: str = "/proc", names: Sequence[str] = ()
) -> list[str]:
    """Running Dolphins that use ``config`` (Linux), or every running Dolphin elsewhere (unknown)."""
    if platform.startswith("win") or not os.path.isdir(proc):
        return running_dolphins(platform, proc, names)
    target = _norm(config)
    users = []
    for pid in dolphin_pids(names, proc):
        paths = process_paths(pid, proc)
        if paths is None or _norm(paths.config) == target:
            users.append(f"pid {pid}")
    return users


def _norm(path: Path) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


# -- ini editing (line based: keeps the user's comments, order and spacing) -----------------


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []


def get_key(lines: Sequence[str], section: str, key: str) -> str | None:
    in_sec = False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_sec = s[1:-1] == section
        elif in_sec and "=" in s and s.split("=", 1)[0].strip() == key:
            return s.split("=", 1)[1].strip()
    return None


def set_key(lines: Sequence[str], section: str, key: str, value: str) -> list[str]:
    """``key = value`` in ``[section]``: replaced in place, else appended to the section,
    else a new section at the end."""
    out: list[str] = []
    in_sec = done = seen = False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            if in_sec and not done:
                blanks = []
                while out and not out[-1].strip():  # before the blank lines that end the section
                    blanks.append(out.pop())
                out.append(f"{key} = {value}")
                out.extend(blanks)
                done = True
            in_sec = s[1:-1] == section
            seen |= in_sec
        elif in_sec and "=" in s and s.split("=", 1)[0].strip() == key:
            if not done:
                out.append(f"{key} = {value}")
                done = True
            continue
        out.append(line)
    if not done:
        if not seen:
            out.append(f"[{section}]")
        out.append(f"{key} = {value}")
    return out


def default_hotkeys(platform: str = sys.platform) -> list[str]:
    """The keyboard hotkeys Dolphin uses when Hotkeys.ini does not exist.

    Dolphin applies its built-in defaults only when Hotkeys.ini is missing or
    empty; as soon as the file has a ``[Hotkeys]`` section, every key absent
    from it is EMPTY (``ControlGroup::LoadConfig`` reads ""). Writing only the EBC
    keys into a fresh file would silently drop F9, F10, F1-F8: these are written
    first (``HotkeyManager::LoadDefaults``; debugger, GBA and Wii remote ones left out).
    """
    win = platform.startswith("win")
    mac = platform == "darwin"
    device = (
        "DInput/0/Keyboard Mouse" if win else "Quartz/0/Keyboard & Mouse" if mac else "XInput2/0/Virtual core pointer"
    )
    lines = [
        "[Hotkeys]",
        f"Device = {device}",
        "General/Open = @(Ctrl+O)",
        "General/Toggle Pause = F10",
        f"General/Stop = {'ESCAPE' if win else 'Escape'}",
        f"General/Toggle Fullscreen = @(Alt+{'RETURN' if win else 'Return'})",
        "General/Take Screenshot = F9",
        f"Emulation Speed/Disable Emulation Speed Limit = {'TAB' if win else 'Tab'}",
    ]
    for i in range(1, 9):
        lines.append(f"Load State/Load State Slot {i} = F{i}")
        lines.append(f"Save State/Save State Slot {i} = @(Shift+F{i})")
    lines += ["Other State Hotkeys/Undo Load State = F12", "Other State Hotkeys/Undo Save State = @(Shift+F12)"]
    return lines


_OUR_BINDING = re.compile(r"\s*\|?\s*`DSUClient/\d+/" + SERVER_NAME + r":[^`]*`")


def binding(button: str) -> str:
    return f"`{DEVICE}:{button}`"


def merged_expression(old: str, button: str) -> str:
    """The user's expression with ours added (``old | `DSUClient/0/EBC:<btn>```), never twice."""
    old = _OUR_BINDING.sub("", old or "").strip().strip("|").strip()
    return f"{old} | {binding(button)}" if old else binding(button)


def entry(port: int) -> str:
    return f"{SERVER_NAME}:{HOST}:{port}"


def planned_files(config: Path, port: int = DEFAULT_PORT, platform: str = sys.platform) -> tuple[dict, list[str]]:
    """New contents of the three files (path -> lines) and notes for the report."""
    notes = []
    files: dict[Path, list[str]] = {}

    dsu = config / "DSUClient.ini"
    lines = read_lines(dsu)
    entries = get_key(lines, "Server", "Entries") or ""
    kept = [e for e in entries.split(";") if e and not e.startswith(f"{SERVER_NAME}:")]
    value = ";".join([*kept, entry(port)]) + ";"
    lines = set_key(set_key(lines, "Server", "Enabled", "True"), "Server", "Entries", value)
    files[dsu] = lines

    hk = config / "Hotkeys.ini"
    lines = read_lines(hk)
    if not any(line.strip() == "[Hotkeys]" for line in lines):
        lines = default_hotkeys(platform) + lines
        notes.append("Hotkeys.ini had no [Hotkeys] section: Dolphin's default keys were written first")
    for key, button in BINDINGS.items():
        lines = set_key(lines, "Hotkeys", key, merged_expression(get_key(lines, "Hotkeys", key) or "", button))
    files[hk] = lines

    ini = config / "Dolphin.ini"
    files[ini] = set_key(read_lines(ini), "General", "HotkeysRequireFocus", "False")
    return files, notes


@dataclass
class InstallResult:
    ok: bool
    message: str
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def install(
    config: Path,
    port: int = DEFAULT_PORT,
    platform: str = sys.platform,
    running: Sequence[str] | None = None,
    dry_run: bool = False,
) -> InstallResult:
    """Write the EBC configuration into the Dolphin config folder ``config``.

    Refused while a Dolphin using it runs (``running``: the list found by
    ``dolphin_uses``, computed when None). Each file is copied to
    ``<file>.ebc-backup`` once, before EBC first touches it (a marker when the
    file did not exist). Idempotent: a second run changes nothing.
    """
    config = Path(config)
    if running is None:
        running = dolphin_uses(config, platform)
    if running:
        return InstallResult(
            False,
            f"Close Dolphin first ({', '.join(running)}): it rewrites its configuration when it exits",
        )
    if not config.parent.is_dir() and not config.is_dir():
        return InstallResult(False, f"Dolphin configuration folder not found: {config}")
    files, notes = planned_files(config, port, platform)
    result = InstallResult(True, "", notes=notes)
    for path, new in files.items():
        text = "\n".join(new) + "\n"
        old = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
        if old == text:
            result.unchanged.append(path.name)
            continue
        result.changed.append(path.name)
        if dry_run:
            continue
        config.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        if not backup.exists():
            if old is None:
                backup.write_text(NO_FILE_MARKER, encoding="utf-8")
            else:
                shutil.copy2(path, backup)
            result.backups.append(backup.name)
        path.write_text(text, encoding="utf-8")
    result.message = (
        f"Dolphin configured for EBC in {config}"
        if result.changed
        else f"Dolphin already configured for EBC ({config})"
    )
    return result


@dataclass
class ConfigStatus:
    dsu_entry: bool
    hotkeys: list[str]  # EBC hotkeys bound (Hotkeys.ini keys)
    focus_off: bool

    @property
    def complete(self) -> bool:
        return self.dsu_entry and len(self.hotkeys) == len(BINDINGS) and self.focus_off

    def summary(self) -> str:
        missing = []
        if not self.dsu_entry:
            missing.append("DSU server entry")
        if len(self.hotkeys) < len(BINDINGS):
            missing.append(f"{len(BINDINGS) - len(self.hotkeys)} hotkeys")
        if not self.focus_off:
            missing.append("hotkeys without window focus")
        return "configured" if not missing else "missing: " + ", ".join(missing)


def status(config: Path, port: int = DEFAULT_PORT) -> ConfigStatus:
    """What the configuration folder holds of the EBC setup (read only)."""
    config = Path(config)
    dsu = read_lines(config / "DSUClient.ini")
    entries = (get_key(dsu, "Server", "Entries") or "").split(";")
    dsu_ok = (get_key(dsu, "Server", "Enabled") or "").lower() == "true" and entry(port) in entries
    hk = read_lines(config / "Hotkeys.ini")
    bound = [k for k, b in BINDINGS.items() if binding(b) in (get_key(hk, "Hotkeys", k) or "")]
    focus = (get_key(read_lines(config / "Dolphin.ini"), "General", "HotkeysRequireFocus") or "").lower() == "false"
    return ConfigStatus(dsu_ok, bound, focus)
