; Generated build-values.iss fixes versions, paths and dependency hashes.
#include "build-values.iss"

[Setup]
AppId={code:InstallIdentity}
AppName=MAA造梦西游4
AppVersion={#ClientVersion}
AppPublisher=MAAZMXY4 contributors
AppPublisherURL=https://github.com/Inkyu-Zero/MAAZMXY4
DefaultDirName={localappdata}\Programs
DefaultGroupName=MAA造梦西游4
DisableDirPage=no
DisableProgramGroupPage=yes
AllowNoIcons=yes
AppendDefaultDirName=no
UsePreviousAppDir=no
UsePreviousLanguage=no
DirExistsWarning=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#IconPath}
UninstallDisplayIcon={app}\MAA造梦西游4.exe
VersionInfoVersion={#FileVersion}
WizardStyle=modern
Compression=lzma2/fast
SolidCompression=yes
CloseApplications=no
RestartApplications=no
AlwaysRestart=no
UninstallLogMode=append
SetupLogging=yes
LicenseFile={#LicensePath}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#DependencyDir}\dotnet-runtime-x64.exe"; Flags: dontcopy
Source: "{#DependencyDir}\vc_redist.x64.exe"; Flags: dontcopy
Source: "install_support.py"; Flags: dontcopy
#include "payload-files.iss"
Source: "{tmp}\migration\interface.json"; DestDir: "{app}"; Flags: external ignoreversion skipifsourcedoesntexist uninsneveruninstall
Source: "{tmp}\migration\config\*"; DestDir: "{app}\config"; Flags: external recursesubdirs createallsubdirs ignoreversion skipifsourcedoesntexist uninsneveruninstall
Source: "{tmp}\migration\recordings\*"; DestDir: "{app}\recordings"; Flags: external recursesubdirs createallsubdirs ignoreversion skipifsourcedoesntexist uninsneveruninstall

[Dirs]
Name: "{app}\runtimes\win-x64\native\plugins"

[Icons]
Name: "{group}\MAA造梦西游4"; Filename: "{app}\MAA造梦西游4.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\MAA造梦西游4"; Filename: "{app}\MAA造梦西游4.exe"; WorkingDir: "{app}"; Tasks: desktopicon

; No automatic launch: installing never connects a game or starts an old queue.
[Code]
#include "policy.iss"

var
  RebootRequired, Normalized, PayloadExtracted: Boolean;
  SelectedDirectory: String;
  DestinationHint: TNewStaticText;

function InstallIdentity(Param: String): String;
begin
  Result := 'MAAZMXY4';
  if Assigned(WizardForm) then Result := Result + '-' + Copy(GetSHA256OfString(Lowercase(WizardDirValue)), 1, 24);
end;

function DotNetReady: Boolean;
var Root: String; Found: TFindRec;
begin
  Result := False;
  if not RegQueryStringValue(HKLM64, 'SOFTWARE\dotnet\Setup\InstalledVersions\x64', 'InstallLocation', Root) then
    Root := ExpandConstant('{commonpf64}\dotnet');
  Root := AddBackslash(Root) + 'shared\Microsoft.NETCore.App\';
  if FindFirst(Root + '10.0.*', Found) then try
    repeat
      if VersionAtLeast(Found.Name, '{#DotNetMinimum}') and
        FileExists(Root + Found.Name + '\coreclr.dll') then begin Result := True; Exit; end;
    until not FindNext(Found);
  finally FindClose(Found); end;
end;

function VcReady: Boolean;
var Installed: Cardinal; Version: String;
begin
  Result := RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and
    (Installed = 1) and RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Version', Version) and
    VersionAtLeast(Version, '{#VcMinimum}');
end;

function InstallDependency(const Name, Hash: String): String;
var FileName: String; Code: Integer; Started: Boolean;
begin
  Result := '';
  ExtractTemporaryFile(Name);
  FileName := ExpandConstant('{tmp}\') + Name;
  if CompareText(GetSHA256OfFile(FileName), Hash) <> 0 then begin
    Result := '依赖安装包校验失败，请重新下载安装器。'; Exit;
  end;
  WizardForm.StatusLabel.Caption := '正在安装运行依赖，请允许 Windows 的权限提示…';
  Log('Installing bundled prerequisite: ' + Name);
  if IsAdmin then
    Started := Exec(FileName, '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, Code)
  else
    Started := ShellExec('runas', FileName, '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, Code);
  if not Started or not DependencyExitOK(Code) then
    Result := '依赖安装未完成（' + Name + '，代码 ' + IntToStr(Code) + '）。客户端尚未覆盖，请允许权限提示后重试。'
  else if Code = 3010 then RebootRequired := True;
end;

procedure UpdateDestination(Sender: TObject);
var Dir: String;
begin
  Dir := WizardForm.DirEdit.Text;
  if not Normalized then Dir := DestinationFor(Dir);
  DestinationHint.Caption := '实际安装目录：' + Dir + #13#10 +
    '识别到旧客户端时保留个人数据并替换旧程序，不备份旧程序；否则新建 MAAZMXY4 子文件夹。';
end;

procedure DirectoryEdited(Sender: TObject);
begin
  Normalized := False; UpdateDestination(Sender);
end;

procedure InitializeWizard;
begin
  DestinationHint := TNewStaticText.Create(WizardForm);
  DestinationHint.Parent := WizardForm.SelectDirPage;
  DestinationHint.SetBounds(WizardForm.DirEdit.Left, WizardForm.DirEdit.Top + ScaleY(68),
    WizardForm.DirEdit.Width + WizardForm.DirBrowseButton.Width, ScaleY(70));
  DestinationHint.AutoSize := False;
  DestinationHint.WordWrap := True;
  WizardForm.DirEdit.OnChange := @DirectoryEdited;
  UpdateDestination(nil);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and not Normalized then begin
    SelectedDirectory := WizardForm.DirEdit.Text;
    WizardForm.DirEdit.Text := DestinationFor(WizardForm.DirEdit.Text);
    Normalized := True; UpdateDestination(nil);
  end;
end;

function RunSupport(const Phase, TargetDir: String): String;
var Code: Integer; SourceDir, Params: String; ErrorText: AnsiString;
begin
  Result := '';
  if not PayloadExtracted then begin
    ExtractTemporaryFiles('{app}\*');
    ExtractTemporaryFile('install_support.py');
    PayloadExtracted := True;
  end;
  SourceDir := ExpandConstant('{tmp}\') + '{app}';
  Params := '-B -E -s -X utf8 "' + ExpandConstant('{tmp}\install_support.py') +
    '" --phase ' + Phase + ' --source "' + SourceDir + '" --target "' + TargetDir +
    '" --output "' + ExpandConstant('{tmp}\migration') + '"';
  if not Exec(SourceDir + '\runtimes\python\python.exe', Params, SourceDir, SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then begin
    ErrorText := ''; LoadStringFromFile(ExpandConstant('{tmp}\install-error.txt'), ErrorText);
    Result := '安装准备未完成：' + UTF8Decode(ErrorText);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not Normalized then begin
    SelectedDirectory := WizardDirValue;
    WizardForm.DirEdit.Text := DestinationFor(WizardDirValue); Normalized := True;
  end;
  if Pos(Lowercase(AddBackslash(ExpandConstant('{win}'))), Lowercase(AddBackslash(WizardDirValue))) = 1 then begin
    Result := '请不要安装到 Windows 系统目录。'; Exit;
  end;
  if not VcReady then Result := InstallDependency('vc_redist.x64.exe', '{#VcHash}');
  if Result = '' then if not VcReady then Result := 'VC++ 运行库尚未就绪，请重启 Windows 后再运行安装器。';
  if Result = '' then if not DotNetReady then Result := InstallDependency('dotnet-runtime-x64.exe', '{#DotNetHash}');
  if Result = '' then if not DotNetReady then Result := '.NET 10 运行库尚未就绪，请重启 Windows 后再运行安装器。';
  if Result <> '' then begin NeedsRestart := RebootRequired; Exit; end;
  Log('Prerequisites ready; no client or game launch requested.');
  Result := RunSupport('plan', SelectedDirectory);
  if Result = '' then begin
    WizardForm.DirEdit.Text := GetIniString('install', 'directory', '', ExpandConstant('{tmp}\install-plan.ini'));
    Normalized := True; UpdateDestination(nil);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var ErrorText: String;
begin
  if CurStep = ssInstall then begin
    ErrorText := RunSupport('install', WizardDirValue);
    if ErrorText <> '' then RaiseException(ErrorText);
  end;
end;

function NeedRestart: Boolean;
begin
  Result := RebootRequired;
end;
