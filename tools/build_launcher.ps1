param(
    [string]$Version = '2.4.3',
    [string]$OutputDirectory = 'dist/launcher'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw '启动器文件版本须为三段数字；候选标识由客户端安装清单保存。' }
$compilerPath = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (-not (Test-Path -LiteralPath $compilerPath)) { throw '需要 Windows 自带 .NET Framework C# 编译器。' }
$outputRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$sourcePath = Join-Path $projectRoot 'tools/launcher/Program.cs'
$iconPath = Join-Path $projectRoot '新图标.ico'
$binaryPath = Join-Path $outputRoot 'MAA造梦西游4.exe'
$versionPath = Join-Path $outputRoot 'BuildVersion.cs'
[IO.File]::WriteAllText($versionPath, "[assembly: System.Reflection.AssemblyVersion(`"$Version.0`")]`n[assembly: System.Reflection.AssemblyFileVersion(`"$Version.0`")]", [Text.UTF8Encoding]::new($false))
& $compilerPath /nologo /target:winexe /optimize+ /platform:anycpu /reference:System.Windows.Forms.dll "/win32icon:$iconPath" "/out:$binaryPath" $sourcePath $versionPath
if ($LASTEXITCODE -ne 0) { throw '启动器编译失败。' }
$receipt = [ordered]@{
    schema = 1
    version = $Version
    source_sha256 = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    icon_sha256 = (Get-FileHash -LiteralPath $iconPath -Algorithm SHA256).Hash.ToLowerInvariant()
    sha256 = (Get-FileHash -LiteralPath $binaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
}
[IO.File]::WriteAllText((Join-Path $outputRoot 'launcher-build.json'), ($receipt | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
Write-Output $binaryPath
