@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title MAA造梦西游4 - 依赖库安装

:: 定义ANSI颜色代码
for /f %%a in ('echo prompt $E^| cmd') do set "ESC=%%a"
set "RESET=%ESC%[0m"
set "GREEN=%ESC%[32m"
set "RED=%ESC%[31m"
set "YELLOW=%ESC%[33m"
set "BLUE=%ESC%[34m"
set "CYAN=%ESC%[36m"
set "WHITE=%ESC%[37m"
set "BOLD=%ESC%[1m"

:: 初始化标志
set "ErrorOccurred=0"
set "UseWinget=1"
set "ForceRepair=0"
if /i "%~1"=="--repair" set "ForceRepair=1"

:: 检测系统架构
set "ARCH=%PROCESSOR_ARCHITECTURE%"
if /i "%ARCH%"=="AMD64" (
    set "ARCH_NAME=x64"
    set "VC_PACKAGE=Microsoft.VCRedist.2015+.x64"
    set "VC_DOWNLOAD=https://aka.ms/vs/17/release/vc_redist.x64.exe"
    set "DOTNET_DOWNLOAD=https://aka.ms/dotnet/10.0/windowsdesktop-runtime-win-x64.exe"
) else if /i "%ARCH%"=="x86" (
    set "ARCH_NAME=x86"
    set "VC_PACKAGE=Microsoft.VCRedist.2015+.x86"
    set "VC_DOWNLOAD=https://aka.ms/vs/17/release/vc_redist.x86.exe"
    set "DOTNET_DOWNLOAD=https://aka.ms/dotnet/10.0/windowsdesktop-runtime-win-x86.exe"
) else if /i "%ARCH%"=="ARM64" (
    set "ARCH_NAME=ARM64"
    set "VC_PACKAGE=Microsoft.VCRedist.2015+.arm64"
    set "VC_DOWNLOAD=https://aka.ms/vs/17/release/vc_redist.arm64.exe"
    set "DOTNET_DOWNLOAD=https://aka.ms/dotnet/10.0/windowsdesktop-runtime-win-arm64.exe"
) else (
    echo %RED%不支持的系统架构: %ARCH%%RESET%
    echo %RED%Unsupported system architecture: %ARCH%%RESET%
    pause
    exit /b 1
)

echo.
echo %CYAN%检测到系统架构 / Detected system architecture: %BOLD%%ARCH_NAME%%RESET%
echo.

:: 检测管理员权限（使用纯 cmd 方式）
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo %YELLOW%====================================================================================================%RESET%
    echo %BOLD%%YELLOW%需要管理员权限！%RESET%
    echo %BOLD%%YELLOW%Administrator privileges required!%RESET%
    echo.
    echo %WHITE%请右键点击此脚本，选择"以管理员身份运行"%RESET%
    echo %WHITE%Please right-click this script and select "Run as administrator"%RESET%
    echo %YELLOW%====================================================================================================%RESET%
    echo.
    pause
    exit /b 1
)

:: 检测 winget 是否可用
where winget >nul 2>&1
if %errorlevel% neq 0 (
    set "UseWinget=0"
) else (
    winget --version >nul 2>&1
    if !errorlevel! neq 0 set "UseWinget=0"
)

:: ============================================
:: 先检测已安装的组件，避免无意义的重复安装
:: ============================================
echo %BLUE%====================================================================================================%RESET%
echo %BOLD%%CYAN%正在检测已安装的组件%RESET%
echo %BLUE%====================================================================================================%RESET%

call :DetectDotNet10
call :DetectVC

if "%DOTNET10_OK%"=="1" (
    echo %GREEN%[已安装]%RESET% .NET Desktop Runtime 10
) else (
    echo %YELLOW%[未安装]%RESET% .NET Desktop Runtime 10
)
if "%VC_OK%"=="1" (
    echo %GREEN%[已安装]%RESET% Visual C++ 运行库 2015-2022
) else (
    echo %YELLOW%[未安装]%RESET% Visual C++ 运行库 2015-2022
)
echo.

if "%ForceRepair%"=="1" (
    echo %YELLOW%已指定 --repair，将强制修复两个组件%RESET%
    echo.
) else if "%DOTNET10_OK%"=="1" if "%VC_OK%"=="1" (
    echo %BOLD%%GREEN%所需的运行库都已在系统中，无需安装。%RESET%
    echo %GREEN%All required runtimes are already installed. Nothing to do.%RESET%
    echo.
    echo %WHITE%如果 MAA 仍然无法启动，可以用下面的命令强制修复：%RESET%
    echo %CYAN%"%~nx0" --repair%RESET%
    echo.
    pause
    exit /b 0
)

:: 设置临时下载目录
set "TEMP_DIR=%TEMP%\MAADependencySetup"
if not exist "%TEMP_DIR%" mkdir "%TEMP_DIR%"

:: 按是否已安装决定参数：首次安装必须用 /install，/repair 只对已安装的产品有效
if "%VC_OK%"=="0" (set "VC_ARG=/install /passive /norestart") else (set "VC_ARG=/repair /passive /norestart")
if "%DOTNET10_OK%"=="0" (set "DOTNET_ARG=/install /passive /norestart") else (set "DOTNET_ARG=/repair /passive /norestart")

:: ============================================
:: Visual C++ 运行库
:: ============================================
if "%VC_OK%"=="0" goto :InstallVC
if "%ForceRepair%"=="1" goto :InstallVC
echo %GREEN%[跳过]%RESET% Visual C++ 运行库已安装
echo.
goto :AfterVC

:InstallVC
echo %BLUE%====================================================================================================%RESET%
echo %BOLD%%CYAN%正在安装 Visual C++ 运行库 2015-2022 (%ARCH_NAME%)%RESET%
echo %BLUE%====================================================================================================%RESET%
echo.

if %UseWinget%==1 (
    winget install "%VC_PACKAGE%" --override "%VC_ARG%" --accept-package-agreements --force
    if !errorlevel! neq 0 (
        set "ErrorOccurred=1"
        echo %YELLOW%winget 安装失败，尝试使用直接下载方式...%RESET%
        call :DownloadAndInstallVC
    )
) else (
    call :DownloadAndInstallVC
)

:AfterVC
echo.

:: ============================================
:: .NET Desktop Runtime 10
:: ============================================
if "%DOTNET10_OK%"=="0" goto :InstallDotNet
if "%ForceRepair%"=="1" goto :InstallDotNet
echo %GREEN%[跳过]%RESET% .NET Desktop Runtime 10 已安装
echo.
goto :AfterDotNet

:InstallDotNet
echo %BLUE%====================================================================================================%RESET%
echo %BOLD%%CYAN%正在安装 .NET Desktop Runtime 10.0 (%ARCH_NAME%)%RESET%
echo %BOLD%%CYAN%Installing .NET Desktop Runtime 10.0 (%ARCH_NAME%)%RESET%
echo %BLUE%====================================================================================================%RESET%
echo.

if %UseWinget%==1 (
    winget install "Microsoft.DotNet.DesktopRuntime.10" --override "%DOTNET_ARG%" --accept-package-agreements --force
    if !errorlevel! neq 0 (
        set "ErrorOccurred=1"
        echo %YELLOW%winget 安装失败，尝试使用直接下载方式...%RESET%
        call :DownloadAndInstallDotNet
    )
) else (
    call :DownloadAndInstallDotNet
)

:AfterDotNet
:: 清理临时文件
if exist "%TEMP_DIR%" rd /s /q "%TEMP_DIR%" >nul 2>&1

:: 重新检测一次，用实际结果说话
call :DetectDotNet10
call :DetectVC

echo.
echo %BLUE%====================================================================================================%RESET%
if "%DOTNET10_OK%"=="1" if "%VC_OK%"=="1" (
    echo %BOLD%%GREEN%运行库安装完成。%RESET%
    echo %BOLD%%GREEN%Runtime libraries installed successfully.%RESET%
    echo.
    echo %WHITE%请重新双击 MAA造梦西游4.exe 启动游戏助手。%RESET%
    echo %WHITE%如果仍然报错，请重启电脑后再试一次。%RESET%
) else (
    echo %BOLD%%RED%安装未完全成功%RESET%
    echo %BOLD%%RED%Installation did not complete successfully%RESET%
    echo.
    if "%DOTNET10_OK%"=="0" echo %RED%  - 仍缺少 .NET Desktop Runtime 10%RESET%
    if "%VC_OK%"=="0" echo %RED%  - 仍缺少 Visual C++ 运行库%RESET%
    echo.
    echo %YELLOW%请手动下载安装：%RESET%
    echo %WHITE%.NET Desktop Runtime 10 (%ARCH_NAME%):%RESET%
    echo %CYAN%%DOTNET_DOWNLOAD%%RESET%
    echo.
    echo %WHITE%Visual C++ 运行库 (%ARCH_NAME%):%RESET%
    echo %CYAN%%VC_DOWNLOAD%%RESET%
)
echo %BLUE%====================================================================================================%RESET%
echo.
pause
exit /b 0

:: ============================================
:: 检测函数
:: ============================================
:DetectDotNet10
set "DOTNET10_OK=0"
for /d %%D in ("%ProgramFiles%\dotnet\shared\Microsoft.NETCore.App\10.*") do set "DOTNET10_OK=1"
if "%DOTNET10_OK%"=="0" (
    for /d %%D in ("%ProgramFiles%\dotnet\shared\Microsoft.WindowsDesktop.App\10.*") do set "DOTNET10_OK=1"
)
if "%DOTNET10_OK%"=="0" (
    for /d %%D in ("%ProgramFiles(x86)%\dotnet\shared\Microsoft.NETCore.App\10.*") do set "DOTNET10_OK=1"
)
exit /b 0

:DetectVC
set "VC_OK=0"
if exist "%SystemRoot%\System32\vcruntime140.dll" if exist "%SystemRoot%\System32\msvcp140.dll" set "VC_OK=1"
exit /b 0

:: ============================================
:: 函数：下载并安装 VC++ Redistributable
:: ============================================
:DownloadAndInstallVC
set "VC_FILE=%TEMP_DIR%\vc_redist.exe"
echo %CYAN%正在下载 VC++ Redistributable...%RESET%
echo %CYAN%Downloading VC++ Redistributable...%RESET%

certutil -urlcache -split -f "%VC_DOWNLOAD%" "%VC_FILE%" >nul 2>&1
if not exist "%VC_FILE%" (
    echo %YELLOW%certutil 下载失败，尝试使用 bitsadmin...%RESET%
    bitsadmin /transfer "VCRedist" /priority high "%VC_DOWNLOAD%" "%VC_FILE%" >nul 2>&1
)
if not exist "%VC_FILE%" (
    echo %RED%下载失败 / Download failed%RESET%
    set "ErrorOccurred=1"
    exit /b 1
)

echo %CYAN%正在安装 VC++ Redistributable (%VC_ARG%)...%RESET%
"%VC_FILE%" %VC_ARG%
if !errorlevel! neq 0 (
    echo %YELLOW%安装返回非零代码: !errorlevel!%RESET%
    set "ErrorOccurred=1"
)
del "%VC_FILE%" >nul 2>&1
exit /b 0

:: ============================================
:: 函数：下载并安装 .NET Desktop Runtime
:: ============================================
:DownloadAndInstallDotNet
set "DOTNET_FILE=%TEMP_DIR%\dotnet_runtime.exe"
echo %CYAN%正在下载 .NET Desktop Runtime 10...%RESET%
echo %CYAN%Downloading .NET Desktop Runtime 10...%RESET%

certutil -urlcache -split -f "%DOTNET_DOWNLOAD%" "%DOTNET_FILE%" >nul 2>&1
if not exist "%DOTNET_FILE%" (
    echo %YELLOW%certutil 下载失败，尝试使用 bitsadmin...%RESET%
    bitsadmin /transfer "DotNetRuntime" /priority high "%DOTNET_DOWNLOAD%" "%DOTNET_FILE%" >nul 2>&1
)
if not exist "%DOTNET_FILE%" (
    echo %RED%下载失败 / Download failed%RESET%
    set "ErrorOccurred=1"
    exit /b 1
)

echo %CYAN%正在安装 .NET Desktop Runtime 10 (%DOTNET_ARG%)...%RESET%
"%DOTNET_FILE%" %DOTNET_ARG%
if !errorlevel! neq 0 (
    echo %YELLOW%安装返回非零代码: !errorlevel!%RESET%
    set "ErrorOccurred=1"
)
del "%DOTNET_FILE%" >nul 2>&1
exit /b 0
