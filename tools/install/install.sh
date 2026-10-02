#!/usr/bin/env bash
# External Brawl Camera (EBC) V3 - one-shot installer for Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/Neyroe/External-Brawl-Camera/project+/tools/install/install.sh | bash
#   tools/install/install.sh [options]          (from a checkout)
#
# Installs (or updates) Blender if needed, the EBC extension, optionally the
# stages scene, and checks the Dolphin side. No sudo, everything goes in your
# home directory. Run with --help for the options. Safe to run again: it updates
# what is already there.
set -euo pipefail

# -- constants (overridable through the environment, mostly for testing) ----------

REPO_SLUG="Neyroe/External-Brawl-Camera"
REPO_URL="${EBC_REPO_URL:-https://github.com/$REPO_SLUG.git}"
GITHUB_API="${EBC_GITHUB_API:-https://api.github.com/repos/$REPO_SLUG}"
SOURCE_REF="${EBC_REF:-project+}"
BLENDER_SERIES="5.2"
BLENDER_INDEX_URL="${EBC_BLENDER_INDEX:-https://download.blender.org/release/Blender$BLENDER_SERIES/}"
BLENDER_MIN="4.2"
EXT_ID="external_brawl_camera"
DEV_LINK_NAME="ebc"
PPLUS_URL="https://projectplusgame.com/"
DME_DEFAULT_NAMES="dolphin-emu dolphin-emu-qt2 dolphin-emu-wx"
# Picked by EBC itself before DME's first lookup (ebc/core/backend.py KNOWN_NAMES).
EBC_KNOWN_NAMES="project-plus-do"

CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/ebc-install"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
OPT_DIR="$HOME/.local/opt"
BIN_DIR="$HOME/.local/bin"

# -- options ------------------------------------------------------------------------

DEV=0
YES=0
NO_SCENE=0
DRY_RUN=0
BLENDER_ARG=""
EBC_VERSION=""
REPO_DIR_ARG=""

usage() {
    cat <<'EOF'
Usage: install.sh [options]

Installs External Brawl Camera V3 for Blender (4.2 or newer).

Options:
  --dev               Developer setup: clone the repository (or use this
                      checkout), create a Python venv with the dev tools and
                      pre-commit, and link the extension to the sources.
  --blender PATH      Use this Blender executable (4.2 or newer).
  --version VER       EBC version to install (release asset or tag vVER).
                      Default: the latest V3 release, else the sources.
  --repo-dir DIR      --dev: where to clone the repository
                      (default: this checkout, else ~/src/External-Brawl-Camera).
  --yes, -y           Answer yes to every question (removal of the V2 add-on).
  --no-scene          Do not download the stages scene.
  --dry-run           Print what would be done, change nothing.
  -h, --help          Show this help.

Environment: EBC_REF (git ref used to build from source, default project+),
EBC_REPO_URL, DME_DOLPHIN_PROCESS_NAME (see README.md).
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dev) DEV=1 ;;
        --blender) BLENDER_ARG="${2:?--blender needs a path}"; shift ;;
        --blender=*) BLENDER_ARG="${1#*=}" ;;
        --version) EBC_VERSION="${2:?--version needs a value}"; shift ;;
        --version=*) EBC_VERSION="${1#*=}" ;;
        --repo-dir) REPO_DIR_ARG="${2:?--repo-dir needs a path}"; shift ;;
        --repo-dir=*) REPO_DIR_ARG="${1#*=}" ;;
        --yes|-y) YES=1 ;;
        --no-scene) NO_SCENE=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done
EBC_VERSION="${EBC_VERSION#v}"

# -- output helpers -----------------------------------------------------------------

step() { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# Run a command that changes something; only print it with --dry-run.
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '    [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

confirm() {
    local prompt="$1" answer=""
    if [[ $YES -eq 1 ]]; then
        return 0
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        info "[dry-run] would ask: $prompt"
        return 1
    fi
    if [[ -r /dev/tty ]] && { : </dev/tty; } 2>/dev/null; then
        read -r -p "    $prompt [y/N] " answer </dev/tty || answer=""
    else
        info "$prompt -> no terminal to ask, skipped (use --yes)."
        return 1
    fi
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# -- pure helpers (no side effects; covered by the tests) ----------------------------

# version_ge A B: true when version A >= version B (dotted numbers).
version_ge() {
    [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

# Latest "X.Y.Z" of a series from a download.blender.org directory listing (stdin).
latest_blender_from_index() {
    local series="$1"
    grep -oE "blender-${series//./\\.}\.[0-9]+-linux-x64\.tar\.xz" \
        | sed -E 's/^blender-([0-9.]+)-linux-x64\.tar\.xz$/\1/' \
        | sort -V | tail -n1
}

blender_archive_name() { printf 'blender-%s-linux-x64.tar.xz' "$1"; }

# Expected SHA-256 of FILE from a blender-X.Y.Z.sha256 listing (stdin).
sha256_from_listing() {
    awk -v f="$1" '$2 == f || $2 == "*" f { print $1; exit }'
}

# Download URLs of every release asset, newest release first (GitHub API JSON on stdin).
asset_urls_from_releases_json() {
    grep -oE '"browser_download_url"[[:space:]]*:[[:space:]]*"[^"]+"' \
        | sed -E 's/.*"(https?:[^"]+)"$/\1/'
}

# First V3 extension zip URL (stdin: asset URLs, newest first). Only names like
# external_brawl_camera-X.Y.Z.zip qualify: the old "EBC 2.0" release (a legacy
# add-on) never matches, and without --version pre-release builds
# (3.1.0-rc1, ...) are skipped.
pick_v3_asset() {
    local version="${1:-}" re
    if [[ -n "$version" ]]; then
        re="/${EXT_ID}-${version//./\\.}\\.zip$"
    else
        re="/${EXT_ID}-[0-9]+\\.[0-9]+\\.[0-9]+\\.zip$"
    fi
    grep -E "$re" | head -n1 || true
}

pick_scene_asset() {
    grep -iE '/ebc_stages[^/]*\.(zip|blend)$' | head -n1 || true
}

# "4.3.2" from the output of `blender --version` (stdin).
parse_blender_version() {
    sed -nE 's/^Blender ([0-9]+\.[0-9]+(\.[0-9]+)?).*/\1/p' | head -n1
}

# -- I/O helpers ---------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

fetch_text() {
    if have curl; then
        curl -fsSL --retry 2 --connect-timeout 15 -H 'User-Agent: ebc-install' "$1"
    elif have wget; then
        wget -qO- --header='User-Agent: ebc-install' "$1"
    else
        die "curl or wget is required."
    fi
}

fetch_file() {
    local url="$1" out="$2"
    local quiet=()
    mkdir -p "$(dirname "$out")"
    if have curl; then
        if [[ -t 2 ]]; then quiet=(--progress-bar); else quiet=(-sS); fi
        curl -fL "${quiet[@]}" --retry 2 --connect-timeout 15 -H 'User-Agent: ebc-install' -o "$out.part" "$url"
    else
        if [[ ! -t 2 ]]; then quiet=(-q); fi
        wget "${quiet[@]}" -O "$out.part" "$url"
    fi
    mv -f "$out.part" "$out"
}

blender_version_of() {
    local exe="$1"
    [[ -x "$exe" ]] || return 1
    "$exe" --version 2>/dev/null </dev/null | parse_blender_version
}

# Run Python inside Blender (headless). Extra env vars can be passed before.
blender_py() {
    local code="$1"; shift
    "$BLENDER" -b --python-exit-code 3 --python-expr "$code" "$@" </dev/null 2>&1
}

move_to_trash() {
    local path="$1" backup
    if have gio && gio trash "$path" 2>/dev/null; then
        info "Moved to the trash: $path"
        return 0
    fi
    backup="$DATA_HOME/ebc-install/backup-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$backup"
    mv "$path" "$backup/"
    info "Moved $path to $backup/"
}

# -- state ---------------------------------------------------------------------------

BLENDER=""
BLENDER_VER=""
BLENDER_SOURCE=""
EXT_DIR=""
USER_DEFAULT_DIR=""
PYVER=""
EXT_MODULE=""
EXT_VERSION=""
SCENE_PATH=""
REPO_DIR=""
VENV_DIR=""
V2_ADDONS=()
V2_DME=()
V2_KEPT=0
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"

# A checkout this script runs from (tools/install/install.sh), if any.
local_checkout() {
    local root
    [[ -n "$SCRIPT_DIR" ]] || return 1
    root="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd)" || return 1
    grep -qs "^id = \"$EXT_ID\"" "$root/ebc/blender_manifest.toml" || return 1
    printf '%s' "$root"
}

manifest_version() {
    sed -nE 's/^version = "([^"]+)".*/\1/p' "$1/ebc/blender_manifest.toml" | head -n1
}

# -- 1. Blender ----------------------------------------------------------------------

try_blender() {
    local exe="$1" ver
    ver="$(blender_version_of "$exe" || true)"
    if [[ -z "$ver" ]]; then
        return 1
    fi
    if ! version_ge "$ver" "$BLENDER_MIN"; then
        info "Skipping $exe (Blender $ver, need $BLENDER_MIN or newer)."
        return 1
    fi
    BLENDER="$(readlink -f "$exe")"
    BLENDER_VER="$ver"
    return 0
}

find_blender() {
    local c
    if [[ -n "$BLENDER_ARG" ]]; then
        [[ -x "$BLENDER_ARG" ]] || die "--blender: $BLENDER_ARG is not an executable."
        try_blender "$BLENDER_ARG" || die "--blender: $BLENDER_ARG is not Blender $BLENDER_MIN or newer."
        BLENDER_SOURCE="--blender"
        return 0
    fi
    local candidates=()
    if have blender; then candidates+=("$(command -v blender)"); fi
    candidates+=("$BIN_DIR/blender")
    # Newest first.
    while IFS= read -r c; do candidates+=("$c"); done < <(
        for c in "$OPT_DIR"/blender-*/blender /opt/blender*/blender /usr/local/blender*/blender \
            /snap/bin/blender; do
            if [[ -x "$c" ]]; then printf '%s\n' "$c"; fi
        done | sort -rV)
    for c in "${candidates[@]}"; do
        if try_blender "$c"; then
            BLENDER_SOURCE="found"
            return 0
        fi
    done
    if have flatpak && flatpak info org.blender.Blender >/dev/null 2>&1; then
        warn "Blender from Flatpak found but not used: its sandbox cannot see the Dolphin process."
    fi
    return 1
}

download_blender() {
    local index ver archive sums expected actual dest tmp
    step "Downloading Blender $BLENDER_SERIES LTS"
    index="$(fetch_text "$BLENDER_INDEX_URL")" || die "Could not read $BLENDER_INDEX_URL"
    ver="$(latest_blender_from_index "$BLENDER_SERIES" <<<"$index")"
    [[ -n "$ver" ]] || die "No Blender $BLENDER_SERIES build listed on $BLENDER_INDEX_URL"
    dest="$OPT_DIR/blender-$ver"
    if [[ -x "$dest/blender" ]]; then
        info "Blender $ver already in $dest"
    else
        archive="$(blender_archive_name "$ver")"
        info "Blender $ver -> $dest"
        if [[ $DRY_RUN -eq 1 ]]; then
            info "[dry-run] download ${BLENDER_INDEX_URL}$archive, check SHA-256, extract"
            BLENDER="$dest/blender"; BLENDER_VER="$ver"; BLENDER_SOURCE="downloaded"
            return 0
        fi
        sums="$(fetch_text "${BLENDER_INDEX_URL}blender-$ver.sha256")" || die "Could not read the SHA-256 list."
        expected="$(sha256_from_listing "$archive" <<<"$sums")"
        [[ -n "$expected" ]] || die "No SHA-256 for $archive in blender-$ver.sha256"
        if [[ -f "$CACHE_DIR/$archive" ]] \
            && [[ "$(sha256sum "$CACHE_DIR/$archive" | cut -d' ' -f1)" == "$expected" ]]; then
            info "Using the cached archive."
        else
            fetch_file "${BLENDER_INDEX_URL}$archive" "$CACHE_DIR/$archive"
        fi
        actual="$(sha256sum "$CACHE_DIR/$archive" | cut -d' ' -f1)"
        if [[ "$actual" != "$expected" ]]; then
            rm -f "$CACHE_DIR/$archive"
            die "SHA-256 mismatch for $archive (expected $expected, got $actual)."
        fi
        info "SHA-256 OK."
        mkdir -p "$OPT_DIR"
        tmp="$(mktemp -d "$OPT_DIR/.blender-$ver.XXXXXX")"
        tar -xJf "$CACHE_DIR/$archive" -C "$tmp" --strip-components=1
        mv "$tmp" "$dest"
        rm -f "$CACHE_DIR/$archive"
    fi
    BLENDER="$dest/blender"
    BLENDER_VER="$ver"
    BLENDER_SOURCE="downloaded"
    create_shortcuts "$dest" "$ver"
}

create_shortcuts() {
    local dir="$1" ver="$2" link="$BIN_DIR/blender" target desktop
    if [[ -e "$link" || -L "$link" ]]; then
        target="$(readlink "$link" || true)"
        if [[ -L "$link" && "$target" == "$OPT_DIR"/blender-* ]]; then
            run ln -sfn "$dir/blender" "$link"
            info "Updated $link"
        else
            info "Left $link as is (not created by this installer)."
        fi
    else
        run mkdir -p "$BIN_DIR"
        run ln -s "$dir/blender" "$link"
        info "Created $link"
    fi
    case ":$PATH:" in *":$BIN_DIR:"*) ;; *) info "Note: $BIN_DIR is not on your PATH." ;; esac

    desktop="$DATA_HOME/applications/blender-$BLENDER_SERIES.desktop"
    if [[ $DRY_RUN -eq 1 ]]; then
        info "[dry-run] write $desktop"
        return 0
    fi
    mkdir -p "$(dirname "$desktop")"
    cat >"$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Blender $BLENDER_SERIES LTS
GenericName=3D modeler
Comment=Blender $ver (installed for External Brawl Camera)
Exec="$dir/blender" %f
Icon=$dir/blender.svg
Terminal=false
Categories=Graphics;3DGraphics;
MimeType=application/x-blender;
StartupWMClass=Blender
EOF
    info "Created $desktop"
    if have update-desktop-database; then
        update-desktop-database "$(dirname "$desktop")" >/dev/null 2>&1 || true
    fi
}

ensure_blender() {
    step "Blender"
    if find_blender; then
        info "Using Blender $BLENDER_VER: $BLENDER"
    else
        info "No Blender $BLENDER_MIN or newer found."
        download_blender
        info "Using Blender $BLENDER_VER: $BLENDER"
    fi
}

# Ask Blender where its user directories are, and look for V2 leftovers.
PY_QUERY='
import bpy, sys, os, addon_utils
def out(k, v):
    print("EBC|%s|%s" % (k, v))
prefs = bpy.context.preferences
repo = next((r for r in prefs.extensions.repos if r.module == "user_default"), None)
ext = bpy.utils.user_resource("EXTENSIONS")
out("ext", ext)
out("user_default", repo.directory if repo else os.path.join(ext, "user_default"))
out("pyver", "%d.%d" % sys.version_info[:2])
ext_real = os.path.realpath(ext) + os.sep
for d in addon_utils.paths():
    if not os.path.isdir(d):
        continue
    for n in sorted(os.listdir(d)):
        init = os.path.join(d, n, "__init__.py")
        try:
            with open(init, encoding="utf-8", errors="replace") as fh:
                txt = fh.read(8000)
        except OSError:
            continue
        if "bl_info" in txt and "\"External-Brawl-Camera\"" in txt:
            out("v2addon", os.path.join(d, n))
seen = set()
for sp in sys.path:
    if not sp or not os.path.isdir(sp):
        continue
    real = os.path.realpath(sp)
    if (real + os.sep).startswith(ext_real) or real in seen:
        continue
    seen.add(real)
    for n in sorted(os.listdir(sp)):
        if n.lower().startswith("dolphin_memory_engine"):
            out("v2dme", os.path.join(sp, n))
'

query_blender() {
    local line key val outp
    V2_ADDONS=()
    V2_DME=()
    if [[ ! -x "$BLENDER" ]]; then
        # Dry run before the download: show the default locations.
        EXT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/blender/$BLENDER_SERIES/extensions"
        USER_DEFAULT_DIR="$EXT_DIR/user_default"
        PYVER="3.13"
        return 0
    fi
    outp="$(blender_py "$PY_QUERY")" || { printf '%s\n' "$outp" >&2; die "Blender failed to start headless."; }
    while IFS= read -r line; do
        [[ "$line" == EBC\|* ]] || continue
        key="${line#EBC|}"; val="${key#*|}"; key="${key%%|*}"
        case "$key" in
            ext) EXT_DIR="$val" ;;
            user_default) USER_DEFAULT_DIR="$val" ;;
            pyver) PYVER="$val" ;;
            v2addon) V2_ADDONS+=("$val") ;;
            v2dme) V2_DME+=("$val") ;;
        esac
    done <<<"$outp"
    [[ -n "$EXT_DIR" && -n "$USER_DEFAULT_DIR" ]] || die "Could not read Blender's user directories."
}

# -- 2. V2 migration -----------------------------------------------------------------

migrate_v2() {
    local f d cfg found=()
    step "Looking for EBC V2 leftovers"
    # Add-on folders of every Blender version (Blender only reports its own).
    cfg="${XDG_CONFIG_HOME:-$HOME/.config}/blender"
    for f in "$cfg"/*/scripts/addons/*/__init__.py \
             ${BLENDER_USER_RESOURCES:+"$BLENDER_USER_RESOURCES"/scripts/addons/*/__init__.py} \
             ${BLENDER_USER_SCRIPTS:+"$BLENDER_USER_SCRIPTS"/addons/*/__init__.py}; do
        [[ -f "$f" ]] || continue
        if grep -qs '"External-Brawl-Camera"' "$f" && grep -qs 'bl_info' "$f"; then
            V2_ADDONS+=("$(dirname "$f")")
        fi
    done
    for d in "${V2_ADDONS[@]}" "${V2_DME[@]}"; do
        d="$(readlink -f "$d" 2>/dev/null || echo "$d")"
        [[ -e "$d" ]] || continue
        local dup=0 x
        for x in "${found[@]}"; do if [[ "$x" == "$d" ]]; then dup=1; fi; done
        if [[ $dup -eq 0 ]]; then found+=("$d"); fi
    done
    if [[ ${#found[@]} -eq 0 ]]; then
        info "None found."
        return 0
    fi
    info "Found (V2 add-on or a dolphin_memory_engine installed by hand into Blender's Python):"
    for d in "${found[@]}"; do info "  $d"; done
    info "V3 bundles its own dolphin_memory_engine; these can conflict with it."
    if confirm "Move them to the trash (or a backup folder)?"; then
        for d in "${found[@]}"; do run move_to_trash "$d"; done
    else
        V2_KEPT=1
        warn "Kept. Remove them yourself if EBC misbehaves (README.md, Installation)."
    fi
}

# -- 3. Extension --------------------------------------------------------------------

PY_DISABLE='
import bpy, os
m = os.environ["EBC_MODULE"]
if m in bpy.context.preferences.addons:
    bpy.ops.preferences.addon_disable(module=m)
    bpy.ops.wm.save_userpref()
'

remove_dev_link() {
    local link="$USER_DEFAULT_DIR/$DEV_LINK_NAME"
    if [[ -L "$link" ]]; then
        info "Removing the development link $link (replaced by the installed zip)."
        if [[ $DRY_RUN -eq 0 ]]; then
            EBC_MODULE="bl_ext.user_default.$DEV_LINK_NAME" blender_py "$PY_DISABLE" >/dev/null || true
        fi
        run rm -f "$link"
    fi
}

# Directory with ebc/ for a build: this checkout, or a downloaded source tree.
source_tree_for_build() {
    local root tag url tarball dest
    if root="$(local_checkout)"; then
        if [[ -z "$EBC_VERSION" || "$(manifest_version "$root")" == "$EBC_VERSION" ]]; then
            info "Building from this checkout: $root" >&2
            printf '%s' "$root"
            return 0
        fi
    fi
    if [[ -n "$EBC_VERSION" ]]; then tag="v$EBC_VERSION"; else tag="$SOURCE_REF"; fi
    dest="$CACHE_DIR/src-$tag"
    url="https://codeload.github.com/$REPO_SLUG/tar.gz/$tag"
    info "Downloading the sources ($tag)" >&2
    if [[ $DRY_RUN -eq 1 ]]; then
        info "[dry-run] download $url" >&2
        printf '%s' "$dest"
        return 0
    fi
    tarball="$CACHE_DIR/src-$tag.tar.gz"
    if ! fetch_file "$url" "$tarball" 2>/dev/null; then
        die "No V3 release on GitHub and no sources at ref '$tag'. Use --version, EBC_REF, or run the script from a checkout."
    fi
    rm -rf "$dest"; mkdir -p "$dest"
    tar -xzf "$tarball" -C "$dest" --strip-components=1
    rm -f "$tarball"
    grep -qs "^id = \"$EXT_ID\"" "$dest/ebc/blender_manifest.toml" \
        || die "Ref '$tag' does not contain the V3 extension (ebc/blender_manifest.toml)."
    printf '%s' "$dest"
}

build_zip() {
    local src="$1" out="$CACHE_DIR/dist" ver
    ver="$(manifest_version "$src" 2>/dev/null || echo unknown)"
    if [[ $DRY_RUN -eq 1 ]]; then
        info "[dry-run] blender --command extension build --source-dir $src/ebc --output-dir $out" >&2
        printf '%s' "$out/$EXT_ID-$ver.zip"
        return 0
    fi
    rm -rf "$out"; mkdir -p "$out"
    "$BLENDER" --factory-startup --command extension build \
        --source-dir "$src/ebc" --output-dir "$out" </dev/null >"$CACHE_DIR/build.log" 2>&1 \
        || { cat "$CACHE_DIR/build.log" >&2; die "extension build failed."; }
    for ver in "$out"/"$EXT_ID"-*.zip; do
        if [[ -f "$ver" ]]; then printf '%s' "$ver"; return 0; fi
    done
}

PY_INSTALL_FALLBACK='
import bpy, os
bpy.ops.extensions.package_install_files(filepath=os.environ["EBC_ZIP"], repo="user_default", enable_on_install=True)
bpy.ops.wm.save_userpref()
'

install_zip() {
    local zip="$1" outp
    info "Installing $zip"
    if [[ $DRY_RUN -eq 1 ]]; then
        info "[dry-run] blender --command extension install-file -r user_default -e $zip"
        return 0
    fi
    if outp="$("$BLENDER" --command extension install-file -r user_default -e "$zip" </dev/null 2>&1)" \
        && grep -qiE 'STATUS (Re)?installed' <<<"$outp"; then
        info "Installed with 'blender --command extension install-file'."
    else
        printf '%s\n' "$outp" | tail -n5 >&2
        warn "install-file failed, trying the Python operator."
        EBC_ZIP="$zip" blender_py "$PY_INSTALL_FALLBACK" >/dev/null || die "Could not install $zip"
    fi
}

install_extension_user() {
    local releases="" urls="" asset zip src
    step "External Brawl Camera extension"
    EXT_MODULE="bl_ext.user_default.$EXT_ID"
    releases="$(fetch_text "$GITHUB_API/releases?per_page=30" 2>/dev/null)" \
        || warn "Could not query GitHub releases; building from source."
    urls="$(asset_urls_from_releases_json <<<"$releases" || true)"
    asset="$(pick_v3_asset "$EBC_VERSION" <<<"$urls")"
    if [[ -n "$asset" ]]; then
        info "Release asset: $asset"
        zip="$CACHE_DIR/$(basename "$asset")"
        run fetch_file "$asset" "$zip"
    else
        if [[ -n "$EBC_VERSION" ]]; then
            info "No release asset $EXT_ID-$EBC_VERSION.zip; building tag v$EBC_VERSION from source."
        else
            info "No V3 release on GitHub yet (the 'EBC 2.0' release is the old V2 add-on); building from source."
        fi
        src="$(source_tree_for_build)"
        zip="$(build_zip "$src")"
        [[ -n "$zip" ]] || die "The build produced no zip (see $CACHE_DIR/build.log)."
    fi
    if [[ $DRY_RUN -eq 0 ]]; then
        "$BLENDER" --factory-startup --command extension validate "$zip" </dev/null >/dev/null 2>&1 \
            || die "$zip is not a valid V3 extension package."
    fi
    EXT_VERSION="$(basename "$zip" .zip)"; EXT_VERSION="${EXT_VERSION#"$EXT_ID"-}"
    remove_dev_link
    install_zip "$zip"
}

# -- 3b. Developer setup -------------------------------------------------------------

find_python() {
    local c v
    for c in python3.13 python3.12 python3.11 python3; do
        have "$c" || continue
        v="$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
        if [[ -n "$v" ]] && version_ge "$v" "3.11"; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

setup_repo() {
    local root
    if [[ -n "$REPO_DIR_ARG" ]]; then
        REPO_DIR="$(readlink -m "$REPO_DIR_ARG")"
    elif root="$(local_checkout)"; then
        REPO_DIR="$root"
        info "Using this checkout: $REPO_DIR"
        return 0
    else
        REPO_DIR="$HOME/src/External-Brawl-Camera"
    fi
    if [[ -d "$REPO_DIR/.git" || -f "$REPO_DIR/.git" ]]; then
        info "Repository: $REPO_DIR"
        if [[ -z "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]]; then
            run git -C "$REPO_DIR" pull --ff-only --quiet || warn "git pull failed; keeping the current state."
        else
            info "Local changes present, not pulling."
        fi
    elif [[ -e "$REPO_DIR" ]]; then
        die "$REPO_DIR exists and is not a git repository (use --repo-dir)."
    else
        have git || die "git is required for --dev."
        info "Cloning $REPO_URL ($SOURCE_REF) into $REPO_DIR"
        run mkdir -p "$(dirname "$REPO_DIR")"
        run git clone --quiet --branch "$SOURCE_REF" "$REPO_URL" "$REPO_DIR" \
            || die "git clone failed (does ref '$SOURCE_REF' exist? set EBC_REF)."
    fi
    if [[ $DRY_RUN -eq 0 ]]; then
        grep -qs "^id = \"$EXT_ID\"" "$REPO_DIR/ebc/blender_manifest.toml" \
            || die "$REPO_DIR has no V3 extension (ebc/blender_manifest.toml)."
    fi
}

setup_venv() {
    local py
    py="$(find_python)" || die "Python 3.11 or newer is required for --dev (python3.11+ on PATH)."
    VENV_DIR="$REPO_DIR/.venv"
    info "Python: $py ($("$py" --version 2>&1))"
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        run "$py" -m venv "$VENV_DIR"
    fi
    if [[ $DRY_RUN -eq 0 && ! -e "$VENV_DIR/.gitignore" ]]; then
        printf '*\n' >"$VENV_DIR/.gitignore"
    fi
    if [[ ! -f "$REPO_DIR/requirements-dev.txt" ]]; then
        info "No requirements-dev.txt in $REPO_DIR: dev tools skipped."
        return 0
    fi
    info "Installing requirements-dev.txt and pre-commit into $VENV_DIR"
    run "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
    run "$VENV_DIR/bin/python" -m pip install --quiet -r "$REPO_DIR/requirements-dev.txt" pre-commit
    run precommit_install || warn "pre-commit install failed (run it from $REPO_DIR: .venv/bin/pre-commit install)."
}

# Subshell: pre-commit works on the git repository of the current directory.
precommit_install() (
    cd "$REPO_DIR" && "$VENV_DIR/bin/pre-commit" install --install-hooks >/dev/null
)

PY_DEV_ENABLE='
import bpy, os
m = os.environ["EBC_MODULE"]
bpy.ops.preferences.addon_enable(module=m)
if m not in bpy.context.preferences.addons:
    raise SystemExit("could not enable " + m)
bpy.ops.wm.save_userpref()
'

# Extract the Linux wheel by hand (fallback, if Blender did not sync it).
PY_WHEEL_FALLBACK='
import os, glob, zipfile
src, dst = os.environ["EBC_WHEELS"], os.environ["EBC_SITE"]
whl = sorted(glob.glob(os.path.join(src, "dolphin_memory_engine-*manylinux*x86_64*.whl")))[-1]
os.makedirs(dst, exist_ok=True)
zipfile.ZipFile(whl).extractall(dst)
print("extracted", whl)
'

install_extension_dev() {
    local link target site
    step "Developer setup"
    setup_repo
    setup_venv
    step "Linking the extension to $REPO_DIR/ebc"
    EXT_MODULE="bl_ext.user_default.$DEV_LINK_NAME"
    EXT_VERSION="$(manifest_version "$REPO_DIR" 2>/dev/null || echo "?") (sources)"
    link="$USER_DEFAULT_DIR/$DEV_LINK_NAME"
    target="$REPO_DIR/ebc"
    if [[ -d "$USER_DEFAULT_DIR/$EXT_ID" && ! -L "$USER_DEFAULT_DIR/$EXT_ID" ]]; then
        info "Removing the zip-installed copy (user_default.$EXT_ID), the link replaces it."
        run "$BLENDER" --command extension remove "user_default.$EXT_ID" </dev/null >/dev/null 2>&1 \
            || run rm -rf "${USER_DEFAULT_DIR:?}/$EXT_ID"
    fi
    if [[ -L "$link" && "$(readlink -f "$link")" == "$(readlink -f "$target")" ]]; then
        info "Link already in place: $link"
    elif [[ -e "$link" && ! -L "$link" ]]; then
        die "$link exists and is not a link; move it away first."
    else
        run mkdir -p "$USER_DEFAULT_DIR"
        run ln -sfn "$target" "$link"
        if [[ $DRY_RUN -eq 0 ]]; then info "Linked $link -> $target"; fi
    fi
    # Blender extracts extension wheels only when the add-on is enabled or when it
    # rebuilds its extension cache; with an up-to-date cache, missing wheels of a
    # linked extension are never extracted again (measured on 4.3.2 and 5.2.2).
    # Dropping the cache and enabling the add-on makes Blender sync them itself
    # into $EXT_DIR/.local/lib/pythonX.Y/site-packages.
    run rm -f "$EXT_DIR/.cache/compat.dat"
    if [[ $DRY_RUN -eq 1 ]]; then
        info "[dry-run] enable $EXT_MODULE in Blender (wheels synced by Blender)"
        return 0
    fi
    EBC_MODULE="$EXT_MODULE" blender_py "$PY_DEV_ENABLE" >/dev/null || die "Could not enable $EXT_MODULE"
    site="$EXT_DIR/.local/lib/python$PYVER/site-packages"
    if ! compgen -G "$site/dolphin_memory_engine-*.dist-info" >/dev/null; then
        warn "Blender did not extract the wheels; extracting the Linux wheel into $site"
        EBC_WHEELS="$REPO_DIR/ebc/wheels" EBC_SITE="$site" blender_py "$PY_WHEEL_FALLBACK" >/dev/null \
            || die "Could not extract the wheel."
    fi
    if [[ -d "${XDG_CACHE_HOME:-$HOME/.cache}/ebc_dev_repo" ]]; then
        info "Note: tools/launch_blender.sh registers a second copy ('EBC dev'); disable one of them."
    fi
}

# -- 4. Scene ------------------------------------------------------------------------

install_scene() {
    local releases urls asset docs dest file
    if [[ $NO_SCENE -eq 1 ]]; then return 0; fi
    step "Stages scene"
    releases="$(fetch_text "$GITHUB_API/releases?per_page=30" 2>/dev/null || true)"
    urls="$(asset_urls_from_releases_json <<<"$releases" || true)"
    asset="$(pick_scene_asset <<<"$urls")"
    if [[ -z "$asset" ]]; then
        info "No stages scene (ebc_stages*.zip or .blend) published in the releases yet."
        info "EBC works without it; you can add your own stage models."
        return 0
    fi
    docs="$(xdg-user-dir DOCUMENTS 2>/dev/null || echo "$HOME/Documents")"
    [[ -d "$docs" && "$docs" != "$HOME" ]] || docs="$HOME"
    dest="$docs/EBC"
    file="$dest/$(basename "$asset")"
    if [[ -f "$file" ]]; then
        info "Already downloaded: $file"
    else
        run fetch_file "$asset" "$file"
    fi
    SCENE_PATH="$file"
    if [[ "$file" == *.zip && $DRY_RUN -eq 0 ]]; then
        if have unzip; then
            unzip -nq "$file" -d "$dest" && SCENE_PATH="$dest"
        elif have python3; then
            # (python's zipfile overwrites: only when nothing was extracted yet)
            if [[ ! -e "$dest/.extracted" ]]; then
                python3 -m zipfile -e "$file" "$dest" && touch "$dest/.extracted"
            fi
            SCENE_PATH="$dest"
        else
            info "Unzip $file yourself."
        fi
    fi
    info "Scene: $SCENE_PATH"
}

# -- 5. Dolphin / Project+ -----------------------------------------------------------

check_dolphin() {
    local found=() f scope comm names=() n pid
    step "Dolphin / Project+"
    if have flatpak; then
        if flatpak info org.DolphinEmu.dolphin-emu >/dev/null 2>&1; then
            found+=("Flatpak org.DolphinEmu.dolphin-emu")
        fi
    fi
    if have dolphin-emu; then found+=("$(command -v dolphin-emu)"); fi
    while IFS= read -r f; do found+=("$f"); done < <(
        find "$HOME" "$HOME/Applications" "$HOME/Downloads" "$HOME/Games" "$HOME/.local/bin" \
            -maxdepth 2 -iname '*.AppImage' 2>/dev/null \
            | grep -iE 'project|pplus|p\+|dolphin' | sort -u || true)
    if [[ ${#found[@]} -eq 0 ]]; then
        info "No Dolphin / Project+ found. Get Project+ (with its Dolphin) from $PPLUS_URL"
        info "You need your own copy of Super Smash Bros. Brawl (NTSC-U, RSBE01)."
    else
        for f in "${found[@]}"; do info "Found: $f"; done
    fi
    for f in "${found[@]}"; do
        if [[ "$f" == Flatpak* ]]; then
            info "Flatpak Dolphin: a native Blender can reach it (same user, ptrace rule below)."
            info "Check its process name while it runs (often dolphin-emu)."
        fi
    done

    # Running Dolphin processes and their names (what DME matches).
    for pid in /proc/[0-9]*; do
        comm="$(cat "$pid/comm" 2>/dev/null || true)"
        if [[ "${comm,,}" == *dolphin* || "$comm" == "$EBC_KNOWN_NAMES" ]]; then names+=("$comm"); fi
    done
    if [[ ${#names[@]} -gt 0 ]]; then
        for n in $(printf '%s\n' "${names[@]}" | sort -u); do
            if [[ " $DME_DEFAULT_NAMES " == *" $n "* ]]; then
                info "Running Dolphin process '$n': matched by default."
            elif [[ "$n" == "$EBC_KNOWN_NAMES" ]]; then
                info "Running Dolphin process '$n' (Project+ 3.2 AppImage): EBC finds it itself on Connect."
            elif [[ "${DME_DOLPHIN_PROCESS_NAME:-}" == "$n" ]]; then
                info "Running Dolphin process '$n': matched (DME_DOLPHIN_PROCESS_NAME)."
            else
                info "Running Dolphin process '$n' is not a default name: start Blender with"
                info "  DME_DOLPHIN_PROCESS_NAME=$n blender"
            fi
        done
    else
        info "If your Dolphin binary is not named dolphin-emu, start Blender with"
        info "  DME_DOLPHIN_PROCESS_NAME=<name from /proc/<pid>/comm> blender"
    fi

    # DME reads Dolphin's memory with process_vm_readv, which needs ptrace
    # access (PTRACE_MODE_ATTACH) to the Dolphin process.
    scope="$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo none)"
    case "$scope" in
        none) info "ptrace: no Yama module, same-user access is allowed. OK." ;;
        0) info "kernel.yama.ptrace_scope = 0: Blender can read Dolphin (same user). OK." ;;
        1)
            warn "kernel.yama.ptrace_scope = 1: a process may only read its own descendants,"
            warn "so Blender cannot read a Dolphin it did not start, and Connect fails."
            warn "Fix (until reboot): sudo sysctl kernel.yama.ptrace_scope=0"
            warn "Permanent: echo 'kernel.yama.ptrace_scope = 0' | sudo tee /etc/sysctl.d/10-ptrace.conf"
            warn "(this relaxes a security setting; alternative: sudo setcap cap_sys_ptrace=ep <blender binary>)" ;;
        2) warn "kernel.yama.ptrace_scope = 2: only processes with CAP_SYS_PTRACE may read others."
           warn "Use: sudo setcap cap_sys_ptrace=ep $BLENDER, or set the scope to 0 (sudo sysctl)." ;;
        3) warn "kernel.yama.ptrace_scope = 3: ptrace disabled until reboot; DME cannot work." ;;
        *) info "kernel.yama.ptrace_scope = $scope" ;;
    esac
}

# -- 6. Final check ------------------------------------------------------------------

PY_VERIFY='
import bpy, os, sys, importlib
m = os.environ["EBC_MODULE"]
ok = m in bpy.context.preferences.addons
print("EBC|enabled|%s" % ok)
try:
    importlib.import_module(m)
    print("EBC|module|ok")
except Exception as ex:
    print("EBC|module|%r" % ex)
try:
    import dolphin_memory_engine as dme
    ver = getattr(dme, "__version__", "")
    if not ver:
        try:
            from dolphin_memory_engine import version as v
            ver = getattr(v, "version", getattr(v, "__version__", "?"))
        except Exception:
            ver = "?"
    print("EBC|dme|%s|%s" % (ver, dme.__file__))
except Exception as ex:
    print("EBC|dme|FAIL|%r" % ex)
'

verify() {
    local outp enabled module dme
    step "Checking the installation"
    if [[ $DRY_RUN -eq 1 ]]; then
        info "[dry-run] start Blender headless and import the extension and dolphin_memory_engine"
        return 0
    fi
    outp="$(EBC_MODULE="$EXT_MODULE" blender_py "$PY_VERIFY" || true)"
    enabled="$(sed -n 's/^EBC|enabled|//p' <<<"$outp")"
    module="$(sed -n 's/^EBC|module|//p' <<<"$outp")"
    dme="$(sed -n 's/^EBC|dme|//p' <<<"$outp")"
    VERIFY_OK=1
    if [[ "$enabled" == "True" && "$module" == "ok" ]]; then
        info "Extension enabled: $EXT_MODULE"
    else
        warn "Extension not enabled or failed to load ($enabled, $module)."
        VERIFY_OK=0
    fi
    if [[ -n "$dme" && "$dme" != FAIL* ]]; then
        info "dolphin_memory_engine ${dme%%|*}: ${dme#*|}"
    else
        warn "import dolphin_memory_engine failed: ${dme#FAIL|}"
        if [[ $V2_KEPT -eq 1 ]]; then
            warn "The V2 leftovers listed above are the likely cause; run again with --yes to remove them."
        fi
        VERIFY_OK=0
    fi
    if [[ $VERIFY_OK -eq 0 ]]; then
        printf '%s\n' "$outp" | tail -n 20 >&2
    fi
}

summary() {
    step "Summary"
    info "Blender:    $BLENDER_VER  $BLENDER ($BLENDER_SOURCE)"
    info "Extension:  External Brawl Camera $EXT_VERSION  ($EXT_MODULE)"
    info "Installed:  $USER_DEFAULT_DIR"
    if [[ -n "$REPO_DIR" ]]; then info "Sources:    $REPO_DIR  (venv: $VENV_DIR)"; fi
    if [[ -n "$SCENE_PATH" ]]; then info "Scene:      $SCENE_PATH"; fi
    printf '\nNext steps:\n'
    printf '  1. Start Project+ in Dolphin and launch a game (emulation running).\n'
    printf '  2. Start Blender%s.\n' "$([[ "$BLENDER_SOURCE" == downloaded ]] && echo " (menu entry 'Blender $BLENDER_SERIES LTS', or $BIN_DIR/blender)")"
    printf '  3. In a 3D viewport press N, open the EBC tab, click Connect.\n'
    if [[ $DEV -eq 1 && -d "$REPO_DIR/tests" ]]; then
        printf '  Dev: source %s/bin/activate; python -m pytest tests\n' "$VENV_DIR"
        printf '       BLENDER=%s tools/blender_isolated.sh -b --factory-startup --python tools/smoke_test.py\n' "$BLENDER"
    fi
    printf 'Troubleshooting: README.md (https://github.com/%s)\n' "$REPO_SLUG"
}

main() {
    VERIFY_OK=1
    [[ "$(uname -s)" == Linux ]] || die "This script is for Linux (use install.ps1 on Windows)."
    [[ "$(uname -m)" == x86_64 ]] || warn "Only x86_64 has a bundled dolphin_memory_engine wheel."
    [[ $EUID -ne 0 ]] || die "Do not run this as root; it installs into your home directory."
    if [[ $DRY_RUN -eq 1 ]]; then info "Dry run: nothing will be changed."; fi
    if [[ $DRY_RUN -eq 0 ]]; then mkdir -p "$CACHE_DIR"; fi
    ensure_blender
    query_blender
    migrate_v2
    if [[ $DEV -eq 1 ]]; then
        install_extension_dev
    else
        install_extension_user
    fi
    install_scene
    check_dolphin
    verify
    summary
    [[ $VERIFY_OK -eq 1 ]] || exit 1
}

# Sourcing the file (tests) only defines the functions.
if [[ "${BASH_SOURCE[0]:-$0}" == "$0" || -z "${BASH_SOURCE[0]:-}" ]]; then
    main
fi
