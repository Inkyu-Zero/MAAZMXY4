<#
给 MAA造梦西游4.exe 做本地自签名，用于消除「无法验证发布者 / 未知发布者」安全警告。

适用场景：已经清除下载标记、也放开了 Internet 区域启动限制之后，Windows 仍以
「此文件没有包含有效的数字签名」为由拦截。

用法（在项目根目录执行）：
    powershell -ExecutionPolicy Bypass -File tools\sign_exe.ps1

注意：
  - 创建证书与签名不需要管理员权限；
  - 导入「受信任的发布者 / 受信任的根证书颁发机构」时，Windows 会弹一次安全确认框，
    需要点「是」；如果没点，签名不会被信任，警告可能依旧存在。
  - 签名会改变 exe 文件，脚本运行前会自动备份成 .exe.bak。
#>
param(
    [string]$ExePath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'MAA造梦西游4.exe'),
    [string]$Subject = 'CN=MAAZMXY4 Local Dev'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Host "找不到 exe：$ExePath"
    exit 1
}
$ExePath = (Resolve-Path -LiteralPath $ExePath).Path
Write-Host "目标文件: $ExePath"

# 0) 备份
$backup = "$ExePath.bak"
if (-not (Test-Path -LiteralPath $backup)) {
    Copy-Item -LiteralPath $ExePath -Destination $backup -Force
    Write-Host "已备份 -> $([System.IO.Path]::GetFileName($backup))"
}

# 1) 创建或复用代码签名证书
$cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
    Where-Object { $_.Subject -eq $Subject } |
    Select-Object -First 1

if (-not $cert) {
    Write-Host "创建自签名代码签名证书..."
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $Subject `
        -CertStoreLocation Cert:\CurrentUser\My `
        -NotAfter (Get-Date).AddYears(5)
    Write-Host "  指纹: $($cert.Thumbprint)"
} else {
    Write-Host "复用已有证书: $($cert.Thumbprint)"
}

# 2) 签名（不加时间戳，避免依赖网络）
$result = Set-AuthenticodeSignature -FilePath $ExePath -Certificate $cert
Write-Host "签名结果: $($result.Status)"

# 3) 导出证书，供导入受信任存储 / 其他机器使用
$cerPath = Join-Path (Split-Path $ExePath -Parent) 'MAAZMXY4-LocalDev.cer'
if (Test-Path -LiteralPath $cerPath) { Remove-Item -LiteralPath $cerPath -Force }
Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
Write-Host "证书已导出: $cerPath"

# 4) 导入受信任存储（可能弹一次系统确认框）
foreach ($store in @('TrustedPublisher', 'Root')) {
    try {
        Import-Certificate -FilePath $cerPath -CertStoreLocation "Cert:\CurrentUser\$store" -ErrorAction Stop | Out-Null
        Write-Host "  已导入 Cert:\CurrentUser\$store"
    } catch {
        Write-Host "  导入 $store 失败（可能在等待确认或被策略阻止）: $($_.Exception.Message)"
    }
}

Write-Host "`n完成。现在双击 exe 测试；若仍弹窗外加："
Write-Host "  1) 确认证书已进入『受信任的发布者』（certmgr.msc 可查看）"
Write-Host "  2) 或把 $cerPath 双击安装到『受信任的根证书颁发机构』"
