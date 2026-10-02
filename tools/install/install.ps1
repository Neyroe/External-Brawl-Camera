<#
.SYNOPSIS
    External Brawl Camera (EBC) V3 - one-shot installer for Windows.

.DESCRIPTION
    Installs (or updates) Blender if needed, the EBC extension, optionally the
    stages scene, and checks the Dolphin side. No administrator rights needed:
    everything goes into your user profile. Safe to run again: it updates what
    is already there. Works with Windows PowerShell 5.1 and PowerShell 7.

    powershell -ExecutionPolicy Bypass -File tools\install\install.ps1 [options]
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Neyroe/External-Brawl-Camera/project+/tools/install/install.ps1))) [options]

.PARAMETER Dev
    Developer setup: clone the repository (or use this checkout), create a
    Python venv with the dev tools and pre-commit, and link the extension to
    the sources with a directory junction.
.PARAMETER Blender
    Use this blender.exe (4.2 or newer).
.PARAMETER Version
    EBC version to install (release asset or tag vVERSION). Default: the latest
    V3 release, else the sources.
.PARAMETER RepoDir
    -Dev: where to clone the repository (default: this checkout, else
    %USERPROFILE%\source\External-Brawl-Camera).
.PARAMETER Yes
    Answer yes to every question (removal of the V2 add-on).
.PARAMETER NoScene
    Do not download the stages scene.
.PARAMETER DryRun
    Print what would be done, change nothing.
#>
# An interactive installer: Write-Host is the intended output channel, and
# -DryRun (not -WhatIf) is how changes are previewed.
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '')]
[CmdletBinding()]
param(
    [switch]$Dev,
    [string]$Blender = '',
    [string]$Version = '',
    [string]$RepoDir = '',
    [switch]$Yes,
    [switch]$NoScene,
    [switch]$DryRun,
    [switch]$Help
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with a progress bar on 5.1

$script:DevMode = [bool]$Dev
$script:YesMode = [bool]$Yes
$script:NoSceneMode = [bool]$NoScene
$script:DryRunMode = [bool]$DryRun
$script:HelpMode = [bool]$Help
$script:BlenderArg = $Blender
$script:EbcVersion = $Version.TrimStart('v')
$script:RepoDirArg = $RepoDir

# -- constants (overridable through the environment, mostly for testing) ----------

function Get-EnvOr([string]$Name, [string]$Default) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($v)) { return $Default }
    return $v
}

$script:RepoSlug = 'Neyroe/External-Brawl-Camera'
$script:RepoUrl = Get-EnvOr 'EBC_REPO_URL' "https://github.com/$script:RepoSlug.git"
$script:GitHubApi = Get-EnvOr 'EBC_GITHUB_API' "https://api.github.com/repos/$script:RepoSlug"
$script:SourceRef = Get-EnvOr 'EBC_REF' 'project+'
$script:BlenderSeries = '5.2'
$script:BlenderIndexUrl = Get-EnvOr 'EBC_BLENDER_INDEX' "https://download.blender.org/release/Blender$script:BlenderSeries/"
$script:BlenderMin = '4.2'
$script:ExtId = 'external_brawl_camera'
$script:DevLinkName = 'ebc'
$script:PPlusUrl = 'https://projectplusgame.com/'
$script:DmeDefaultNames = @('Dolphin', 'DolphinQt2', 'DolphinWx')

$script:LocalAppData = Get-EnvOr 'LOCALAPPDATA' (Join-Path $HOME 'AppData\Local')
$script:AppData = Get-EnvOr 'APPDATA' (Join-Path $HOME 'AppData\Roaming')
$script:CacheDir = Join-Path $script:LocalAppData 'ebc-install'
$script:ProgramsDir = Join-Path $script:LocalAppData 'Programs'

# -- output helpers -------------------------------------------------------------

function Write-Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" }
function Write-Info([string]$Text) { Write-Host "    $Text" }
function Write-Warn([string]$Text) { Write-Host "    WARNING: $Text" -ForegroundColor Yellow }
function Stop-Install([string]$Text) { throw "EBC install: $Text" }

function Test-DryRun { return [bool]$script:DryRunMode }

# Run a script block that changes something; only describe it with -DryRun.
function Invoke-Change([string]$Description, [scriptblock]$Action) {
    if (Test-DryRun) {
        Write-Info "[dry-run] $Description"
        return
    }
    & $Action
}

function Confirm-Action([string]$Prompt) {
    if ($script:YesMode) { return $true }
    if (Test-DryRun) { Write-Info "[dry-run] would ask: $Prompt"; return $false }
    try {
        if (-not [Environment]::UserInteractive -or [Console]::IsInputRedirected) {
            Write-Info "$Prompt -> no console to ask, skipped (use -Yes)."
            return $false
        }
    } catch { Write-Verbose 'Console state unknown; asking anyway.' }
    $answer = Read-Host "    $Prompt [y/N]"
    return ($answer -match '^(y|yes)$')
}

# -- pure helpers (no side effects; covered by the tests) ----------------------------

# Parse "4.3", "4.3.2" into a [version] (missing parts are 0).
function ConvertTo-Version([string]$Text) {
    if ($Text -notmatch '^\d+(\.\d+){0,3}$') { return $null }
    $parts = @($Text.Split('.'))
    while ($parts.Count -lt 3) { $parts += '0' }
    return [version]($parts -join '.')
}

function Test-VersionAtLeast([string]$Have, [string]$Need) {
    $h = ConvertTo-Version $Have
    $n = ConvertTo-Version $Need
    if ($null -eq $h -or $null -eq $n) { return $false }
    return ($h -ge $n)
}

# Latest "X.Y.Z" of a series from a download.blender.org directory listing.
function Get-LatestBlenderFromIndex([string]$Html, [string]$Series) {
    $pattern = 'blender-(' + [regex]::Escape($Series) + '\.\d+)-windows-x64\.zip'
    $found = @([regex]::Matches($Html, $pattern) | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
    if ($found.Count -eq 0) { return '' }
    return ($found | Sort-Object { ConvertTo-Version $_ } | Select-Object -Last 1)
}

function Get-BlenderArchiveName([string]$Ver) { return "blender-$Ver-windows-x64.zip" }

# Expected SHA-256 of a file from a blender-X.Y.Z.sha256 listing.
function Get-Sha256FromListing([string]$Listing, [string]$FileName) {
    foreach ($line in ($Listing -split "`r?`n")) {
        $cols = @($line.Trim() -split '\s+')
        if ($cols.Count -ge 2 -and ($cols[1] -eq $FileName -or $cols[1] -eq "*$FileName")) {
            return $cols[0].ToLowerInvariant()
        }
    }
    return ''
}

# Download URLs of every release asset, newest release first (GitHub API JSON text).
function Get-AssetUrlsFromReleasesJson([string]$Json) {
    if ([string]::IsNullOrWhiteSpace($Json)) { return @() }
    $releases = @(ConvertFrom-Json -InputObject $Json)
    # 5.1 returns the whole array as one object: unroll it.
    if ($releases.Count -eq 1 -and $releases[0] -is [array]) { $releases = @($releases[0]) }
    $urls = New-Object System.Collections.Generic.List[string]
    foreach ($r in $releases) {
        if ($null -eq $r -or -not ($r.PSObject.Properties.Name -contains 'assets')) { continue }
        foreach ($a in @($r.assets)) {
            if ($null -ne $a -and $a.PSObject.Properties.Name -contains 'browser_download_url') {
                $urls.Add([string]$a.browser_download_url)
            }
        }
    }
    return , $urls.ToArray()
}

# First V3 extension zip URL. Only names like external_brawl_camera-X.Y.Z.zip
# qualify: the old "EBC 2.0" release (a legacy add-on) never matches, and
# without -Version pre-release builds (3.1.0-rc1, ...) are skipped.
function Select-V3Asset([string[]]$Urls, [string]$Ver) {
    if ($Ver) {
        $re = '/' + [regex]::Escape("$script:ExtId-$Ver.zip") + '$'
    } else {
        $re = '/' + $script:ExtId + '-\d+\.\d+\.\d+\.zip$'
    }
    foreach ($u in @($Urls)) { if ($u -cmatch $re) { return $u } }
    return ''
}

function Select-SceneAsset([string[]]$Urls) {
    foreach ($u in @($Urls)) { if ($u -match '/ebc_stages[^/]*\.(zip|blend)$') { return $u } }
    return ''
}

# "4.3.2" from the output of `blender --version`.
function Get-BlenderVersionFromText([string]$Text) {
    $m = [regex]::Match($Text, '(?m)^Blender (\d+\.\d+(\.\d+)?)')
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
}

function Get-SourceZipUrl([string]$Ref) { return "https://codeload.github.com/$script:RepoSlug/zip/$Ref" }

function Get-ManifestVersion([string]$ManifestText) {
    $m = [regex]::Match($ManifestText, '(?m)^version = "([^"]+)"')
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
}

function Test-V2AddonInit([string]$InitText) {
    return ($InitText -match 'bl_info' -and $InitText -match '"External-Brawl-Camera"')
}

# -- native commands ------------------------------------------------------------

# Run an executable, return @{ Code; Output }. stderr is merged; on 5.1 a
# native stderr line would otherwise become a terminating error.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @Arguments 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    return @{ Code = $code; Output = (@($out) -join "`n") }
}

function Get-BlenderVersion([string]$Exe) {
    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) { return '' }
    try {
        $r = Invoke-Native $Exe @('--factory-startup', '--version')
        return (Get-BlenderVersionFromText $r.Output)
    } catch { return '' }
}

# Run Python inside Blender (headless). The code goes through a file (no
# quoting issues with 5.1) and results through a UTF-8 file (no console
# code page issues with non-ASCII user names). Returns the result lines.
function Invoke-BlenderPython([string]$Code, [hashtable]$EnvVars) {
    if (-not (Test-Path -LiteralPath $script:CacheDir)) { New-Item -ItemType Directory -Path $script:CacheDir | Out-Null }
    $py = Join-Path $script:CacheDir ('run-' + [guid]::NewGuid().ToString('N') + '.py')
    $outFile = "$py.out"
    $prelude = "import os as _o`n_ebc_out = open(_o.environ['EBC_OUT'], 'w', encoding='utf-8')`ndef out(k, v):`n    _ebc_out.write('EBC|%s|%s\n' % (k, v)); _ebc_out.flush()`n"
    [IO.File]::WriteAllText($py, $prelude + $Code, (New-Object Text.UTF8Encoding($false)))
    $saved = @{}
    $all = @{ EBC_OUT = $outFile }
    if ($EnvVars) { foreach ($k in $EnvVars.Keys) { $all[$k] = $EnvVars[$k] } }
    foreach ($k in $all.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, [string]$all[$k]) }
    try {
        $r = Invoke-Native $script:BlenderExe @('-b', '--python-exit-code', '3', '--python', $py)
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    }
    $lines = @()
    if (Test-Path -LiteralPath $outFile) {
        $lines = @(Get-Content -LiteralPath $outFile -Encoding UTF8)
        Remove-Item -LiteralPath $outFile -Force
    }
    Remove-Item -LiteralPath $py -Force -ErrorAction SilentlyContinue
    return @{ Code = $r.Code; Output = $r.Output; Lines = $lines }
}

function Get-ResultValue([string[]]$Lines, [string]$Key) {
    $prefix = "EBC|$Key|"
    return @($Lines | Where-Object { $_.StartsWith($prefix) } | ForEach-Object { $_.Substring($prefix.Length) })
}

function Save-Url([string]$Url, [string]$OutFile) {
    $dir = Split-Path -Parent $OutFile
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $part = "$OutFile.part"
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $r = Invoke-Native $curl.Source @('-fL', '--retry', '2', '-sS', '-A', 'ebc-install', '-o', $part, $Url)
        if ($r.Code -ne 0) { Stop-Install "download failed: $Url ($($r.Output))" }
    } else {
        Invoke-WebRequest -Uri $Url -OutFile $part -UseBasicParsing -UserAgent 'ebc-install'
    }
    Move-Item -LiteralPath $part -Destination $OutFile -Force
}

function Get-UrlText([string]$Url) {
    $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -UserAgent 'ebc-install' -Headers @{ Accept = 'application/vnd.github+json' }
    if ($resp.Content -is [byte[]]) { return [Text.Encoding]::UTF8.GetString($resp.Content) }
    return [string]$resp.Content
}

function Move-ToRecycleBin([string]$Path) {
    try {
        Add-Type -AssemblyName Microsoft.VisualBasic
        if (Test-Path -LiteralPath $Path -PathType Container) {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($Path, 'OnlyErrorDialogs', 'SendToRecycleBin')
        } else {
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($Path, 'OnlyErrorDialogs', 'SendToRecycleBin')
        }
        Write-Info "Moved to the Recycle Bin: $Path"
    } catch {
        $backup = Join-Path $script:CacheDir ('backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        Move-Item -LiteralPath $Path -Destination $backup
        Write-Info "Moved $Path to $backup"
    }
}

# A junction is removed without touching its target (Remove-Item -Recurse on
# 5.1 would delete the sources behind it).
function Remove-Junction([string]$Path) {
    [IO.Directory]::Delete($Path, $false)
}

function Get-LinkTarget([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item -or -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return $null }
    $t = $item.Target
    if ($t -is [array]) { $t = $t[0] }
    return [string]$t
}

# -- state ----------------------------------------------------------------------

$script:BlenderExe = ''
$script:BlenderVer = ''
$script:BlenderSource = ''
$script:ExtDir = ''
$script:UserDefaultDir = ''
$script:PyVer = ''
$script:ExtModule = ''
$script:ExtVersion = ''
$script:ScenePath = ''
$script:RepoPath = ''
$script:VenvDir = ''
$script:V2Found = @()
$script:V2Kept = $false
$script:VerifyOk = $true
$script:ScriptPath = $PSCommandPath

function Get-LocalCheckout {
    if (-not $script:ScriptPath) { return '' }
    $root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $script:ScriptPath))
    $manifest = Join-Path $root 'ebc\blender_manifest.toml'
    if ((Test-Path -LiteralPath $manifest) -and ((Get-Content -LiteralPath $manifest -Raw) -match "(?m)^id = `"$script:ExtId`"")) {
        return $root
    }
    return ''
}

# -- 1. Blender -----------------------------------------------------------------

function Test-BlenderCandidate([string]$Exe) {
    $ver = Get-BlenderVersion $Exe
    if (-not $ver) { return $false }
    if (-not (Test-VersionAtLeast $ver $script:BlenderMin)) {
        Write-Info "Skipping $Exe (Blender $ver, need $script:BlenderMin or newer)."
        return $false
    }
    $script:BlenderExe = (Resolve-Path -LiteralPath $Exe).Path
    $script:BlenderVer = $ver
    return $true
}

function Find-Blender {
    if ($script:BlenderArg) {
        if (-not (Test-Path -LiteralPath $script:BlenderArg -PathType Leaf)) { Stop-Install "-Blender: $script:BlenderArg not found." }
        if (-not (Test-BlenderCandidate $script:BlenderArg)) { Stop-Install "-Blender: $script:BlenderArg is not Blender $script:BlenderMin or newer." }
        $script:BlenderSource = '-Blender'
        return $true
    }
    $candidates = New-Object System.Collections.Generic.List[string]
    $cmd = Get-Command blender.exe -ErrorAction SilentlyContinue
    if ($cmd) { $candidates.Add($cmd.Source) }
    $globs = @(
        (Join-Path $script:ProgramsDir 'Blender-*\blender.exe'),
        (Join-Path $env:ProgramFiles 'Blender Foundation\Blender*\blender.exe')
    )
    $pf86 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    if ($pf86) { $globs += (Join-Path $pf86 'Steam\steamapps\common\Blender\blender.exe') }
    if ($env:ProgramFiles) { $globs += (Join-Path $env:ProgramFiles 'Steam\steamapps\common\Blender\blender.exe') }
    $found = @()
    foreach ($g in $globs) {
        if (-not $g) { continue }
        $found += @(Get-ChildItem -Path $g -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
    }
    # Newest folder name first (Blender 5.2 before Blender 4.3).
    $found | Sort-Object -Descending | ForEach-Object { $candidates.Add($_) }
    foreach ($c in $candidates) {
        if (Test-BlenderCandidate $c) { $script:BlenderSource = 'found'; return $true }
    }
    return $false
}

function Install-BlenderPortable {
    Write-Step "Downloading Blender $script:BlenderSeries LTS"
    $index = Get-UrlText $script:BlenderIndexUrl
    $ver = Get-LatestBlenderFromIndex $index $script:BlenderSeries
    if (-not $ver) { Stop-Install "No Blender $script:BlenderSeries build listed on $script:BlenderIndexUrl" }
    $dest = Join-Path $script:ProgramsDir "Blender-$ver"
    $exe = Join-Path $dest 'blender.exe'
    if (Test-Path -LiteralPath $exe) {
        Write-Info "Blender $ver already in $dest"
    } else {
        $archive = Get-BlenderArchiveName $ver
        Write-Info "Blender $ver -> $dest"
        if (Test-DryRun) {
            Write-Info "[dry-run] download $script:BlenderIndexUrl$archive, check SHA-256, extract"
            $script:BlenderExe = $exe; $script:BlenderVer = $ver; $script:BlenderSource = 'downloaded'
            return
        }
        $sums = Get-UrlText "$($script:BlenderIndexUrl)blender-$ver.sha256"
        $expected = Get-Sha256FromListing $sums $archive
        if (-not $expected) { Stop-Install "No SHA-256 for $archive in blender-$ver.sha256" }
        $zip = Join-Path $script:CacheDir $archive
        $haveCached = (Test-Path -LiteralPath $zip) -and ((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -eq $expected)
        if ($haveCached) { Write-Info 'Using the cached archive.' } else { Save-Url "$($script:BlenderIndexUrl)$archive" $zip }
        $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            Remove-Item -LiteralPath $zip -Force
            Stop-Install "SHA-256 mismatch for $archive (expected $expected, got $actual)."
        }
        Write-Info 'SHA-256 OK.'
        $tmp = Join-Path $script:ProgramsDir ".blender-$ver-tmp"
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null
        $tar = Get-Command tar.exe -ErrorAction SilentlyContinue
        if ($tar) {
            $r = Invoke-Native $tar.Source @('-xf', $zip, '-C', $tmp)
            if ($r.Code -ne 0) { Stop-Install "extraction failed: $($r.Output)" }
        } else {
            Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
        }
        $inner = @(Get-ChildItem -LiteralPath $tmp -Directory)
        if ($inner.Count -ne 1) { Stop-Install "unexpected layout in $archive" }
        Move-Item -LiteralPath $inner[0].FullName -Destination $dest
        Remove-Item -LiteralPath $tmp -Recurse -Force
        Remove-Item -LiteralPath $zip -Force
    }
    $script:BlenderExe = $exe
    $script:BlenderVer = $ver
    $script:BlenderSource = 'downloaded'
    New-BlenderShortcut $dest $ver
}

function New-BlenderShortcut([string]$Dir, [string]$Ver) {
    $menu = Join-Path $script:AppData 'Microsoft\Windows\Start Menu\Programs'
    $lnk = Join-Path $menu "Blender $script:BlenderSeries LTS.lnk"
    $target = Join-Path $Dir 'blender-launcher.exe'
    if (-not (Test-Path -LiteralPath $target)) { $target = Join-Path $Dir 'blender.exe' }
    Invoke-Change "create the Start menu shortcut $lnk" {
        if (-not (Test-Path -LiteralPath $menu)) { New-Item -ItemType Directory -Path $menu -Force | Out-Null }
        $shell = New-Object -ComObject WScript.Shell
        $s = $shell.CreateShortcut($lnk)
        $s.TargetPath = $target
        $s.WorkingDirectory = $Dir
        $s.IconLocation = (Join-Path $Dir 'blender.exe') + ',0'
        $s.Description = "Blender $Ver (installed for External Brawl Camera)"
        $s.Save()
        Write-Info "Created the Start menu shortcut: $lnk"
    }
}

$script:PyQuery = @'
import bpy, sys, os, addon_utils
prefs = bpy.context.preferences
repo = next((r for r in prefs.extensions.repos if r.module == "user_default"), None)
ext = bpy.utils.user_resource("EXTENSIONS")
out("ext", ext)
out("user_default", repo.directory if repo else os.path.join(ext, "user_default"))
out("pyver", "%d.%d" % sys.version_info[:2])
ext_real = os.path.normcase(os.path.realpath(ext)) + os.sep
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
    real = os.path.normcase(os.path.realpath(sp))
    if (real + os.sep).startswith(ext_real) or real in seen:
        continue
    seen.add(real)
    for n in sorted(os.listdir(sp)):
        if n.lower().startswith("dolphin_memory_engine"):
            out("v2dme", os.path.join(sp, n))
'@

function Get-BlenderInfo {
    if (-not (Test-Path -LiteralPath $script:BlenderExe)) {
        # Dry run before the download: show the default locations.
        $script:ExtDir = Join-Path $script:AppData "Blender Foundation\Blender\$script:BlenderSeries\extensions"
        $script:UserDefaultDir = Join-Path $script:ExtDir 'user_default'
        $script:PyVer = '3.13'
        return @()
    }
    $r = Invoke-BlenderPython $script:PyQuery @{}
    $script:ExtDir = @(Get-ResultValue $r.Lines 'ext') | Select-Object -First 1
    $script:UserDefaultDir = @(Get-ResultValue $r.Lines 'user_default') | Select-Object -First 1
    $script:PyVer = @(Get-ResultValue $r.Lines 'pyver') | Select-Object -First 1
    if (-not $script:ExtDir -or -not $script:UserDefaultDir) {
        Write-Host $r.Output
        Stop-Install "Could not read Blender's user directories."
    }
    return @(Get-ResultValue $r.Lines 'v2addon') + @(Get-ResultValue $r.Lines 'v2dme')
}

# -- 2. V2 migration ------------------------------------------------------------

function Invoke-V2Migration([string[]]$FromBlender) {
    Write-Step 'Looking for EBC V2 leftovers'
    $found = New-Object System.Collections.Generic.List[string]
    foreach ($p in @($FromBlender)) { if ($p -and (Test-Path -LiteralPath $p) -and -not $found.Contains($p)) { $found.Add($p) } }
    # Add-on folders of every Blender version (Blender only reports its own).
    $base = Join-Path $script:AppData 'Blender Foundation\Blender'
    foreach ($init in @(Get-ChildItem -Path (Join-Path $base '*\scripts\addons\*\__init__.py') -ErrorAction SilentlyContinue)) {
        $text = Get-Content -LiteralPath $init.FullName -Raw -ErrorAction SilentlyContinue
        if ($text -and (Test-V2AddonInit $text)) {
            $d = $init.Directory.FullName
            if (-not $found.Contains($d)) { $found.Add($d) }
        }
    }
    if ($found.Count -eq 0) { Write-Info 'None found.'; return }
    Write-Info "Found (V2 add-on or a dolphin_memory_engine installed by hand into Blender's Python):"
    foreach ($d in $found) { Write-Info "  $d" }
    Write-Info 'V3 bundles its own dolphin_memory_engine; these can conflict with it.'
    if (Confirm-Action 'Move them to the Recycle Bin (or a backup folder)?') {
        foreach ($d in $found) { $x = $d; Invoke-Change "move $x to the Recycle Bin" { Move-ToRecycleBin $x } }
    } else {
        $script:V2Kept = $true
        Write-Warn 'Kept. Remove them yourself if EBC misbehaves (README.md, Installation).'
    }
}

# -- 3. Extension ---------------------------------------------------------------

$script:PyDisable = @'
import bpy, os
m = os.environ["EBC_MODULE"]
if m in bpy.context.preferences.addons:
    bpy.ops.preferences.addon_disable(module=m)
    bpy.ops.wm.save_userpref()
'@

function Remove-DevLink {
    $link = Join-Path $script:UserDefaultDir $script:DevLinkName
    if ($null -ne (Get-LinkTarget $link)) {
        Write-Info "Removing the development link $link (replaced by the installed zip)."
        if (-not (Test-DryRun)) {
            Invoke-BlenderPython $script:PyDisable @{ EBC_MODULE = "bl_ext.user_default.$script:DevLinkName" } | Out-Null
            Remove-Junction $link
        }
    }
}

function Get-SourceTree {
    $root = Get-LocalCheckout
    if ($root) {
        $mv = Get-ManifestVersion (Get-Content -LiteralPath (Join-Path $root 'ebc\blender_manifest.toml') -Raw)
        if (-not $script:EbcVersion -or $mv -eq $script:EbcVersion) {
            Write-Info "Building from this checkout: $root"
            return $root
        }
    }
    if ($script:EbcVersion) { $tag = "v$script:EbcVersion" } else { $tag = $script:SourceRef }
    $dest = Join-Path $script:CacheDir "src-$tag"
    $url = Get-SourceZipUrl $tag
    Write-Info "Downloading the sources ($tag)"
    if (Test-DryRun) { Write-Info "[dry-run] download $url"; return $dest }
    $zip = "$dest.zip"
    try { Save-Url $url $zip } catch {
        Stop-Install "No V3 release on GitHub and no sources at ref '$tag'. Use -Version, EBC_REF, or run the script from a checkout."
    }
    if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
    $tmp = "$dest.tmp"
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    $inner = @(Get-ChildItem -LiteralPath $tmp -Directory)[0]
    Move-Item -LiteralPath $inner.FullName -Destination $dest
    Remove-Item -LiteralPath $tmp -Recurse -Force
    Remove-Item -LiteralPath $zip -Force
    $manifest = Join-Path $dest 'ebc\blender_manifest.toml'
    if (-not (Test-Path -LiteralPath $manifest)) { Stop-Install "Ref '$tag' does not contain the V3 extension (ebc\blender_manifest.toml)." }
    return $dest
}

function New-ExtensionZip([string]$Src) {
    $outDir = Join-Path $script:CacheDir 'dist'
    $mv = '?'
    $manifest = Join-Path $Src 'ebc\blender_manifest.toml'
    if (Test-Path -LiteralPath $manifest) { $mv = Get-ManifestVersion (Get-Content -LiteralPath $manifest -Raw) }
    $zip = Join-Path $outDir "$script:ExtId-$mv.zip"
    if (Test-DryRun) {
        Write-Info "[dry-run] blender --command extension build --source-dir $Src\ebc --output-dir $outDir"
        return $zip
    }
    if (Test-Path -LiteralPath $outDir) { Remove-Item -LiteralPath $outDir -Recurse -Force }
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    $r = Invoke-Native $script:BlenderExe @('--factory-startup', '--command', 'extension', 'build', '--source-dir', (Join-Path $Src 'ebc'), '--output-dir', $outDir)
    $built = @(Get-ChildItem -Path (Join-Path $outDir "$script:ExtId-*.zip") -ErrorAction SilentlyContinue)
    if ($r.Code -ne 0 -or $built.Count -eq 0) { Write-Host $r.Output; Stop-Install 'extension build failed.' }
    return $built[0].FullName
}

$script:PyInstallFallback = @'
import bpy, os
bpy.ops.extensions.package_install_files(filepath=os.environ["EBC_ZIP"], repo="user_default", enable_on_install=True)
bpy.ops.wm.save_userpref()
'@

function Install-ExtensionZip([string]$Zip) {
    Write-Info "Installing $Zip"
    if (Test-DryRun) { Write-Info "[dry-run] blender --command extension install-file -r user_default -e $Zip"; return }
    $r = Invoke-Native $script:BlenderExe @('--command', 'extension', 'install-file', '-r', 'user_default', '-e', $Zip)
    if ($r.Code -eq 0 -and $r.Output -match 'STATUS (Re)?installed') {
        Write-Info "Installed with 'blender --command extension install-file'."
    } else {
        Write-Host $r.Output
        Write-Warn 'install-file failed, trying the Python operator.'
        $p = Invoke-BlenderPython $script:PyInstallFallback @{ EBC_ZIP = $Zip }
        if ($p.Code -ne 0) { Write-Host $p.Output; Stop-Install "Could not install $Zip" }
    }
}

function Install-ExtensionUser {
    Write-Step 'External Brawl Camera extension'
    $script:ExtModule = "bl_ext.user_default.$script:ExtId"
    $urls = @()
    try { $urls = Get-AssetUrlsFromReleasesJson (Get-UrlText "$script:GitHubApi/releases?per_page=30") }
    catch { Write-Warn "Could not query GitHub releases ($($_.Exception.Message)); building from source." }
    $asset = Select-V3Asset $urls $script:EbcVersion
    if ($asset) {
        Write-Info "Release asset: $asset"
        $zip = Join-Path $script:CacheDir ([IO.Path]::GetFileName($asset))
        $u = $asset
        Invoke-Change "download $u" { Save-Url $u $zip }
    } else {
        if ($script:EbcVersion) {
            Write-Info "No release asset $script:ExtId-$script:EbcVersion.zip; building tag v$script:EbcVersion from source."
        } else {
            Write-Info "No V3 release on GitHub yet (the 'EBC 2.0' release is the old V2 add-on); building from source."
        }
        $zip = New-ExtensionZip (Get-SourceTree)
    }
    if (-not (Test-DryRun)) {
        $v = Invoke-Native $script:BlenderExe @('--factory-startup', '--command', 'extension', 'validate', $zip)
        if ($v.Code -ne 0) { Write-Host $v.Output; Stop-Install "$zip is not a valid V3 extension package." }
    }
    $script:ExtVersion = [IO.Path]::GetFileNameWithoutExtension($zip).Substring($script:ExtId.Length + 1)
    Remove-DevLink
    Install-ExtensionZip $zip
}

# -- 3b. Developer setup --------------------------------------------------------

function Find-Python {
    $tries = @(
        @('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'),
        @('python3.13'), @('python3.12'), @('python3.11'), @('python'), @('python3')
    )
    foreach ($t in $tries) {
        $cmd = Get-Command $t[0] -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # Skip the Microsoft Store "python.exe" stub (it opens the Store).
        if ($cmd.Source -like '*\WindowsApps\*') { continue }
        $a = @($t | Select-Object -Skip 1) + @('-c', 'import sys; print("%d.%d" % sys.version_info[:2])')
        $r = Invoke-Native $cmd.Source $a
        $v = ($r.Output -split "`n" | Select-Object -Last 1).Trim()
        if ($r.Code -eq 0 -and (Test-VersionAtLeast $v '3.11')) {
            $res = @($cmd.Source) + @($t | Select-Object -Skip 1)
            return , $res
        }
    }
    return $null
}

function Initialize-Repo {
    if ($script:RepoDirArg) {
        $script:RepoPath = [IO.Path]::GetFullPath($script:RepoDirArg)
    } else {
        $root = Get-LocalCheckout
        if ($root) { $script:RepoPath = $root; Write-Info "Using this checkout: $root"; return }
        $script:RepoPath = Join-Path $HOME 'source\External-Brawl-Camera'
    }
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath (Join-Path $script:RepoPath '.git')) {
        Write-Info "Repository: $script:RepoPath"
        if ($git) {
            $st = Invoke-Native $git.Source @('-C', $script:RepoPath, 'status', '--porcelain')
            if (-not $st.Output.Trim()) {
                Invoke-Change 'git pull --ff-only' {
                    $p = Invoke-Native $git.Source @('-C', $script:RepoPath, 'pull', '--ff-only', '--quiet')
                    if ($p.Code -ne 0) { Write-Warn 'git pull failed; keeping the current state.' }
                }
            } else { Write-Info 'Local changes present, not pulling.' }
        }
    } elseif (Test-Path -LiteralPath $script:RepoPath) {
        Stop-Install "$script:RepoPath exists and is not a git repository (use -RepoDir)."
    } else {
        if (-not $git) { Stop-Install 'git is required for -Dev (https://git-scm.com/download/win).' }
        Write-Info "Cloning $script:RepoUrl ($script:SourceRef) into $script:RepoPath"
        Invoke-Change "git clone --branch $script:SourceRef $script:RepoUrl $script:RepoPath" {
            $c = Invoke-Native $git.Source @('clone', '--quiet', '--branch', $script:SourceRef, $script:RepoUrl, $script:RepoPath)
            if ($c.Code -ne 0) { Write-Host $c.Output; Stop-Install "git clone failed (does ref '$script:SourceRef' exist? set EBC_REF)." }
        }
    }
    if (-not (Test-DryRun) -and -not (Test-Path -LiteralPath (Join-Path $script:RepoPath 'ebc\blender_manifest.toml'))) {
        Stop-Install "$script:RepoPath has no V3 extension (ebc\blender_manifest.toml)."
    }
}

function Initialize-Venv {
    $py = Find-Python
    if ($null -eq $py) { Stop-Install 'Python 3.11 or newer is required for -Dev (https://www.python.org/downloads/, or: winget install Python.Python.3.13).' }
    $script:VenvDir = Join-Path $script:RepoPath '.venv'
    $vpy = Join-Path $script:VenvDir 'Scripts\python.exe'
    Write-Info "Python: $($py -join ' ')"
    if (-not (Test-Path -LiteralPath $vpy)) {
        Invoke-Change "python -m venv $script:VenvDir" {
            $r = Invoke-Native $py[0] (@($py | Select-Object -Skip 1) + @('-m', 'venv', $script:VenvDir))
            if ($r.Code -ne 0) { Write-Host $r.Output; Stop-Install 'venv creation failed.' }
        }
    }
    $gi = Join-Path $script:VenvDir '.gitignore'
    if (-not (Test-DryRun) -and -not (Test-Path -LiteralPath $gi)) { [IO.File]::WriteAllText($gi, "*`n") }
    if (-not (Test-Path -LiteralPath (Join-Path $script:RepoPath 'requirements-dev.txt'))) {
        Write-Info "No requirements-dev.txt in $($script:RepoPath): dev tools skipped."
        return
    }
    Write-Info "Installing requirements-dev.txt and pre-commit into $script:VenvDir"
    Invoke-Change 'pip install -r requirements-dev.txt pre-commit' {
        $null = Invoke-Native $vpy @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')
        $r = Invoke-Native $vpy @('-m', 'pip', 'install', '--quiet', '-r', (Join-Path $script:RepoPath 'requirements-dev.txt'), 'pre-commit')
        if ($r.Code -ne 0) { Write-Host $r.Output; Stop-Install 'pip install failed.' }
    }
    Invoke-Change 'pre-commit install' {
        Push-Location -LiteralPath $script:RepoPath
        try {
            $r = Invoke-Native (Join-Path $script:VenvDir 'Scripts\pre-commit.exe') @('install', '--install-hooks')
            if ($r.Code -ne 0) { Write-Warn "pre-commit install failed (run it from $script:RepoPath)." }
        } finally { Pop-Location }
    }
}

$script:PyDevEnable = @'
import bpy, os
m = os.environ["EBC_MODULE"]
bpy.ops.preferences.addon_enable(module=m)
if m not in bpy.context.preferences.addons:
    raise SystemExit("could not enable " + m)
bpy.ops.wm.save_userpref()
'@

# Extract the Windows wheel by hand (fallback, if Blender did not sync it).
$script:PyWheelFallback = @'
import os, glob, zipfile
src, dst = os.environ["EBC_WHEELS"], os.environ["EBC_SITE"]
whl = sorted(glob.glob(os.path.join(src, "dolphin_memory_engine-*win_amd64.whl")))[-1]
os.makedirs(dst, exist_ok=True)
zipfile.ZipFile(whl).extractall(dst)
out("extracted", whl)
'@

function Install-ExtensionDev {
    Write-Step 'Developer setup'
    Initialize-Repo
    Initialize-Venv
    $target = Join-Path $script:RepoPath 'ebc'
    Write-Step "Linking the extension to $target"
    $script:ExtModule = "bl_ext.user_default.$script:DevLinkName"
    $manifest = Join-Path $target 'blender_manifest.toml'
    $mv = '?'
    if (Test-Path -LiteralPath $manifest) { $mv = Get-ManifestVersion (Get-Content -LiteralPath $manifest -Raw) }
    $script:ExtVersion = "$mv (sources)"
    $zipCopy = Join-Path $script:UserDefaultDir $script:ExtId
    if ((Test-Path -LiteralPath $zipCopy) -and $null -eq (Get-LinkTarget $zipCopy)) {
        Write-Info "Removing the zip-installed copy (user_default.$script:ExtId), the link replaces it."
        Invoke-Change "blender --command extension remove user_default.$script:ExtId" {
            $null = Invoke-Native $script:BlenderExe @('--command', 'extension', 'remove', "user_default.$script:ExtId")
            if (Test-Path -LiteralPath $zipCopy) { Remove-Item -LiteralPath $zipCopy -Recurse -Force }
        }
    }
    $link = Join-Path $script:UserDefaultDir $script:DevLinkName
    $current = Get-LinkTarget $link
    $want = [IO.Path]::GetFullPath($target).TrimEnd('\')
    if ($null -ne $current -and ([IO.Path]::GetFullPath($current).TrimEnd('\') -eq $want)) {
        Write-Info "Link already in place: $link"
    } elseif ($null -eq $current -and (Test-Path -LiteralPath $link)) {
        Stop-Install "$link exists and is not a link; move it away first."
    } else {
        Invoke-Change "junction $link -> $target" {
            if ($null -ne $current) { Remove-Junction $link }
            if (-not (Test-Path -LiteralPath $script:UserDefaultDir)) { New-Item -ItemType Directory -Path $script:UserDefaultDir -Force | Out-Null }
            # A junction (mklink /J) needs no administrator rights nor developer mode.
            try {
                New-Item -ItemType Junction -Path $link -Target $target | Out-Null
            } catch {
                $r = Invoke-Native "$env:ComSpec" @('/c', 'mklink', '/J', $link, $target)
                if ($r.Code -ne 0) { Write-Host $r.Output; Stop-Install 'Could not create the junction.' }
            }
            Write-Info "Linked $link -> $target"
        }
    }
    # Blender extracts extension wheels only when the add-on is enabled or when it
    # rebuilds its extension cache; with an up-to-date cache, missing wheels of a
    # linked extension are never extracted again (measured on 4.3.2 and 5.2.2).
    # Dropping the cache and enabling the add-on makes Blender sync them itself.
    $compat = Join-Path $script:ExtDir '.cache\compat.dat'
    Invoke-Change "delete $compat" { if (Test-Path -LiteralPath $compat) { Remove-Item -LiteralPath $compat -Force } }
    if (Test-DryRun) { Write-Info "[dry-run] enable $script:ExtModule in Blender (wheels synced by Blender)"; return }
    $e = Invoke-BlenderPython $script:PyDevEnable @{ EBC_MODULE = $script:ExtModule }
    if ($e.Code -ne 0) { Write-Host $e.Output; Stop-Install "Could not enable $script:ExtModule" }
    $site = Join-Path $script:ExtDir ".local\lib\python$script:PyVer\site-packages"
    if (-not (Get-ChildItem -Path (Join-Path $site 'dolphin_memory_engine-*.dist-info') -ErrorAction SilentlyContinue)) {
        Write-Warn "Blender did not extract the wheels; extracting the Windows wheel into $site"
        $w = Invoke-BlenderPython $script:PyWheelFallback @{ EBC_WHEELS = (Join-Path $target 'wheels'); EBC_SITE = $site }
        if ($w.Code -ne 0) { Write-Host $w.Output; Stop-Install 'Could not extract the wheel.' }
    }
}

# -- 4. Scene -------------------------------------------------------------------

function Install-Scene {
    if ($script:NoSceneMode) { return }
    Write-Step 'Stages scene'
    $urls = @()
    try { $urls = Get-AssetUrlsFromReleasesJson (Get-UrlText "$script:GitHubApi/releases?per_page=30") } catch { Write-Verbose "GitHub API: $($_.Exception.Message)" }
    $asset = Select-SceneAsset $urls
    if (-not $asset) {
        Write-Info 'No stages scene (ebc_stages*.zip or .blend) published in the releases yet.'
        Write-Info 'EBC works without it; you can add your own stage models.'
        return
    }
    $docs = [Environment]::GetFolderPath('MyDocuments')
    if (-not $docs) { $docs = $HOME }
    $dest = Join-Path $docs 'EBC'
    $file = Join-Path $dest ([IO.Path]::GetFileName([Uri]::UnescapeDataString($asset)))
    if (Test-Path -LiteralPath $file) { Write-Info "Already downloaded: $file" }
    else { $a = $asset; Invoke-Change "download $a" { Save-Url $a $file } }
    $script:ScenePath = $file
    if ($file -like '*.zip' -and -not (Test-DryRun)) {
        $marker = Join-Path $dest '.extracted'
        # Never overwrite a scene the user may have edited.
        if (-not (Test-Path -LiteralPath $marker)) {
            Expand-Archive -LiteralPath $file -DestinationPath $dest
            [IO.File]::WriteAllText($marker, '')
        }
        $script:ScenePath = $dest
    }
    Write-Info "Scene: $script:ScenePath"
}

# -- 5. Dolphin / Project+ ------------------------------------------------------

function Test-Dolphin {
    Write-Step 'Dolphin / Project+'
    $roots = @(
        [Environment]::GetFolderPath('Desktop'),
        [Environment]::GetFolderPath('MyDocuments'),
        (Join-Path $HOME 'Downloads'),
        $script:ProgramsDir,
        $script:AppData,
        $env:ProgramFiles,
        [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $found = @()
    $cmd = Get-Command Dolphin.exe -ErrorAction SilentlyContinue
    if ($cmd) { $found += $cmd.Source }
    foreach ($r in $roots) {
        $found += @(Get-ChildItem -LiteralPath $r -Filter 'Dolphin*.exe' -Recurse -Depth 3 -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^Dolphin(Qt2|Wx)?\.exe$' } | ForEach-Object { $_.FullName })
    }
    $found = @($found | Sort-Object -Unique)
    if ($found.Count -eq 0) {
        Write-Info "No Dolphin.exe found. Get Project+ (with its Dolphin) from $script:PPlusUrl"
        Write-Info 'You need your own copy of Super Smash Bros. Brawl (NTSC-U, RSBE01).'
    } else {
        foreach ($f in $found) { Write-Info "Found: $f" }
    }
    $procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like '*dolphin*' } | ForEach-Object { $_.ProcessName } | Sort-Object -Unique)
    foreach ($n in $procs) {
        if ($script:DmeDefaultNames -contains $n) {
            Write-Info "Running Dolphin process '$n.exe': matched by default."
        } elseif ($env:DME_DOLPHIN_PROCESS_NAME -and ($env:DME_DOLPHIN_PROCESS_NAME -replace '\.exe$', '') -eq $n) {
            Write-Info "Running Dolphin process '$n.exe': matched (DME_DOLPHIN_PROCESS_NAME)."
        } else {
            Write-Info "Running Dolphin process '$n.exe' is not a default name. Set it once, then restart Blender:"
            Write-Info "  [Environment]::SetEnvironmentVariable('DME_DOLPHIN_PROCESS_NAME', '$n.exe', 'User')"
        }
    }
    if ($procs.Count -eq 0) {
        Write-Info 'If your Dolphin is not named Dolphin.exe, set the user variable DME_DOLPHIN_PROCESS_NAME to its name.'
    }
    Write-Info 'Run Blender and Dolphin with the same rights (both normal, or both as administrator).'
}

# -- 6. Final check -------------------------------------------------------------

$script:PyVerify = @'
import bpy, os, sys, importlib
m = os.environ["EBC_MODULE"]
out("enabled", m in bpy.context.preferences.addons)
try:
    importlib.import_module(m)
    out("module", "ok")
except Exception as ex:
    out("module", repr(ex))
try:
    import dolphin_memory_engine as dme
    ver = getattr(dme, "__version__", "")
    if not ver:
        try:
            from dolphin_memory_engine import version as v
            ver = getattr(v, "version", getattr(v, "__version__", "?"))
        except Exception:
            ver = "?"
    out("dme", "%s|%s" % (ver, dme.__file__))
except Exception as ex:
    out("dme", "FAIL|%r" % ex)
'@

function Test-Installation {
    Write-Step 'Checking the installation'
    if (Test-DryRun) { Write-Info '[dry-run] start Blender headless and import the extension and dolphin_memory_engine'; return }
    $r = Invoke-BlenderPython $script:PyVerify @{ EBC_MODULE = $script:ExtModule }
    $enabled = @(Get-ResultValue $r.Lines 'enabled') | Select-Object -First 1
    $module = @(Get-ResultValue $r.Lines 'module') | Select-Object -First 1
    $dme = @(Get-ResultValue $r.Lines 'dme') | Select-Object -First 1
    if ($enabled -eq 'True' -and $module -eq 'ok') {
        Write-Info "Extension enabled: $script:ExtModule"
    } else {
        Write-Warn "Extension not enabled or failed to load ($enabled, $module)."
        $script:VerifyOk = $false
    }
    if ($dme -and -not $dme.StartsWith('FAIL')) {
        $parts = $dme.Split('|', 2)
        Write-Info "dolphin_memory_engine $($parts[0]): $($parts[1])"
    } else {
        Write-Warn "import dolphin_memory_engine failed: $dme"
        if ($script:V2Kept) { Write-Warn 'The V2 leftovers listed above are the likely cause; run again with -Yes to remove them.' }
        $script:VerifyOk = $false
    }
    if (-not $script:VerifyOk) { Write-Host $r.Output }
}

function Write-Summary {
    Write-Step 'Summary'
    Write-Info "Blender:    $script:BlenderVer  $script:BlenderExe ($script:BlenderSource)"
    Write-Info "Extension:  External Brawl Camera $script:ExtVersion  ($script:ExtModule)"
    Write-Info "Installed:  $script:UserDefaultDir"
    if ($script:RepoPath) { Write-Info "Sources:    $script:RepoPath  (venv: $script:VenvDir)" }
    if ($script:ScenePath) { Write-Info "Scene:      $script:ScenePath" }
    Write-Host ''
    Write-Host 'Next steps:'
    Write-Host '  1. Start Project+ in Dolphin and launch a game (emulation running).'
    if ($script:BlenderSource -eq 'downloaded') {
        Write-Host "  2. Start Blender (Start menu: 'Blender $script:BlenderSeries LTS')."
    } else {
        Write-Host '  2. Start Blender.'
    }
    Write-Host '  3. In a 3D viewport press N, open the EBC tab, click Connect.'
    if ($script:DevMode -and $script:RepoPath -and (Test-Path -LiteralPath (Join-Path $script:RepoPath 'tests'))) {
        Write-Host "  Dev: $script:VenvDir\Scripts\Activate.ps1; python -m pytest tests"
        Write-Host "       `$env:BLENDER_USER_RESOURCES = (New-Item -ItemType Directory -Path (Join-Path `$env:TEMP ('ebc-profile-' + [guid]::NewGuid()))).FullName; & '$script:BlenderExe' -b --factory-startup --python tools\smoke_test.py"
    }
    Write-Host "Troubleshooting: README.md (https://github.com/$script:RepoSlug)"
}

function Invoke-Main {
    if ($script:HelpMode) { Get-Help -Detailed $script:ScriptPath | Out-Host; return }

    if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Stop-Install 'This script is for Windows (use install.sh on Linux).' }
    if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { Write-Warn 'Only x64 Windows has a bundled dolphin_memory_engine wheel.' }
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { Write-Verbose 'TLS 1.2 already the default.' }
    if (Test-DryRun) { Write-Info 'Dry run: nothing will be changed.' }
    if (-not (Test-DryRun) -and -not (Test-Path -LiteralPath $script:CacheDir)) { New-Item -ItemType Directory -Path $script:CacheDir -Force | Out-Null }

    Write-Step 'Blender'
    if (-not (Find-Blender)) {
        Write-Info "No Blender $script:BlenderMin or newer found."
        Install-BlenderPortable
    }
    Write-Info "Using Blender $($script:BlenderVer): $script:BlenderExe"
    $leftovers = Get-BlenderInfo
    Invoke-V2Migration $leftovers
    if ($script:DevMode) { Install-ExtensionDev } else { Install-ExtensionUser }
    Install-Scene
    Test-Dolphin
    Test-Installation
    Write-Summary
}

# Dot-sourcing the file (tests) only defines the functions.
if ($MyInvocation.InvocationName -ne '.' -and -not $env:EBC_INSTALL_NO_MAIN) {
    try {
        # Steps report through Write-Host; anything else they emit is dropped.
        Invoke-Main | Out-Null
    } catch {
        $script:VerifyOk = $false
        Write-Host ''
        Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    }
    if (-not $script:VerifyOk -and $PSCommandPath) { exit 1 }
}
