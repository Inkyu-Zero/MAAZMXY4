[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$OutputDirectory, [switch]$BuildTests)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$out = [IO.Path]::GetFullPath($OutputDirectory)
if ($out.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
    $out.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Place native build artifacts outside the source tree.'
}
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'Visual C++ x64 build tools are required.' }
$vcvars = Join-Path $vs 'VC/Auxiliary/Build/vcvars64.bat'
New-Item -ItemType Directory -Force -Path $out | Out-Null
$source = Join-Path $root 'native/background_mouse_guard/guard.cpp'
$script = "@echo off`r`ncall `"$vcvars`" >nul`r`nif errorlevel 1 exit /b 1`r`ncl /nologo /LD /O2 /MT /W4 /WX /EHsc `"$source`" user32.lib /link /OUT:MaaBackgroundMouseGuard.dll`r`n"
if ($BuildTests) {
    New-Item -ItemType Directory -Force -Path (Join-Path $out 'tests') | Out-Null
    $testSource = Join-Path $root 'tests/native'
    $script += "if errorlevel 1 exit /b 1`r`npushd tests`r`ncl /nologo /LD /MT /W4 /WX /EHsc `"$testSource/air_module_stub.cpp`" /link /OUT:`"Adobe AIR.dll`"`r`nif errorlevel 1 exit /b 1`r`ncl /nologo /MT /W4 /WX /EHsc `"$testSource/mouse_guard_host.cpp`" user32.lib /link /OUT:mouse_guard_host.exe`r`nif errorlevel 1 exit /b 1`r`npopd`r`n"
}
[IO.File]::WriteAllText((Join-Path $out 'build.cmd'), $script, [Text.Encoding]::ASCII)
Push-Location $out
try {
    & cmd /d /c build.cmd
    if ($LASTEXITCODE -ne 0) { throw 'Native mouse guard build failed.' }
    $manifest = @{ schema=1; source_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash.ToLower();
        builder_sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $PSCommandPath).Hash.ToLower();
        sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath './MaaBackgroundMouseGuard.dll').Hash.ToLower(); architecture='x64'; protocol=1 }
    $manifest | ConvertTo-Json | Set-Content -Encoding UTF8 './mouse-guard-build.json'
} finally { Pop-Location }
