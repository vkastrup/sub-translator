<#
    subtrans installer (Windows) — the counterpart to install.sh.

        .\install.ps1
        .\install.ps1 -Resolve
        .\install.ps1 -Resolve -Provider mistral -Key sk-xxxx

    The -Provider/-Key form is the one to use when setting up an edit suite: it writes the
    studio key into .env so a producer never has to create or paste an API key.

    PowerShell blocks unsigned scripts by default, so run it as:

        powershell -ExecutionPolicy Bypass -File .\install.ps1 -Resolve -Provider mistral -Key sk-xxxx

    Installing the Resolve plugin writes into %PROGRAMDATA%, which usually needs an
    elevated shell — "Run as Administrator". Everything else works as a normal user.
#>

[CmdletBinding()]
param(
    [switch]$Resolve,
    [string]$Provider = "",
    [string]$Key = ""
)

$ErrorActionPreference = "Stop"
$Here = $PSScriptRoot
Set-Location $Here

# Plugins folder per Resolve's own Workflow Integrations README. Studio only.
$PluginDir = Join-Path $env:PROGRAMDATA "Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins"
$VenvPython = Join-Path $Here "venv\Scripts\python.exe"

function Invoke-Checked {
    <# Run a native command and stop on a non-zero exit code: $ErrorActionPreference does
       not apply to native exit codes, only to PowerShell errors. #>
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

function Find-BootstrapPython {
    <# The interpreter used once, to build the venv.

       'py' is the launcher that ships with python.org installs and is the reliable one.
       A bare 'python' may be the Microsoft Store alias stub, which prints nothing and
       exits non-zero — so probe by actually asking for a version rather than trusting
       Get-Command. #>
    foreach ($cand in @(
        @{ Exe = "py";      Pre = @("-3") },
        @{ Exe = "python";  Pre = @() },
        @{ Exe = "python3"; Pre = @() }
    )) {
        $exe = $cand.Exe
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        $probe = @($cand.Pre) + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
        $version = (& $exe @probe 2>$null) | Select-Object -First 1
        if ($LASTEXITCODE -ne 0 -or -not $version) { continue }
        $parts = $version.Trim().Split(".")
        if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) { continue }
        return @{ Exe = $cand.Exe; Pre = @($cand.Pre); Version = $version.Trim() }
    }
    return $null
}

$py = Find-BootstrapPython
if (-not $py) {
    throw "no Python 3.9+ found. Install it from https://www.python.org/downloads/windows/ " +
          "and tick 'Add python.exe to PATH'."
}

Write-Host "==> creating venv (Python $($py.Version))"
Invoke-Checked $py.Exe (@($py.Pre) + @("-m", "venv", "venv")) "venv creation"
Invoke-Checked $VenvPython @("-m", "pip", "install", "--quiet", "--upgrade", "pip") "pip upgrade"
Write-Host "==> installing dependencies"
Invoke-Checked $VenvPython @("-m", "pip", "install", "--quiet", "-r", "requirements.txt") "dependency install"
Invoke-Checked $VenvPython @("-c", "import anthropic, openai, pysubs2, pycaption") "dependency check"
Write-Host "    ok"

# ── credentials ───────────────────────────────────────────────────────────────
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }

if ($Provider) {
    # The .env rewrite lives in subtrans/config.py (stdlib only) so this script and
    # install.sh cannot drift on which env var a given provider actually reads.
    Invoke-Checked $VenvPython @(
        "-c",
        "import sys; from subtrans.config import write_env; print('==> ' + write_env(*sys.argv[1:]))",
        $Provider, $Key
    ) "writing .env"

    # The nearest thing to chmod 600: drop inherited access, grant this user only.
    try {
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        icacls .env /inheritance:r /grant:r "${me}:(R,W)" | Out-Null
    } catch {
        Write-Warning "could not restrict permissions on .env — it holds an API key, check it by hand"
    }
}

& $VenvPython -m subtrans.cli --list-providers *> $null
if ($LASTEXITCODE -ne 0) { Write-Warning "could not read the provider registry" }

# ── Resolve plugin ────────────────────────────────────────────────────────────
if ($Resolve) {
    if (-not (Test-Path $PluginDir)) {
        throw "$PluginDir does not exist — is DaVinci Resolve Studio installed? " +
              "(Workflow Integrations are Studio-only.)"
    }
    # The plugin needs to know where this checkout lives; patch the constant on install.
    # It is a raw string literal in the source, so a Windows path goes in verbatim without
    # its backslashes being read as escapes.
    $source = Get-Content (Join-Path $Here "resolve\SubTranslator.py") -Raw -Encoding UTF8
    $literal = 'SUBTRANS_DIR = r"' + $Here.TrimEnd("\") + '"'
    $patched = [regex]::Replace($source, '(?m)^SUBTRANS_DIR = .*$', $literal.Replace('$', '$$'))

    $target = Join-Path $PluginDir "SubTranslator.py"
    try {
        # WriteAllText with an explicit no-BOM encoding: Set-Content -Encoding UTF8 on
        # Windows PowerShell writes a BOM, and Resolve's script loader chokes on it.
        [System.IO.File]::WriteAllText($target, $patched, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        # %PROGRAMDATA% is writable by the installer that made the folder, not necessarily
        # by whoever runs this. A plain catch: a .NET UnauthorizedAccessException arrives
        # wrapped, so a typed catch is not dependable here.
        throw "cannot write to $PluginDir ($($_.Exception.Message)) — " +
              "re-run this script from an Administrator PowerShell."
    }
    Write-Host "==> installed plugin to Workflow Integration Plugins\"
    Write-Host "    restart Resolve, then: Workspace > Workflow Integrations > SubTranslator"
}

Write-Host ""
Write-Host "Configured engines:"
(& $VenvPython -m subtrans.cli --list-providers 2>$null) | Select-String "ready"
Write-Host ""
Write-Host "  venv\Scripts\python -m subtrans.cli --input in.srt --target sv"
Write-Host "  venv\Scripts\python -m subtrans.cli --list-providers"
