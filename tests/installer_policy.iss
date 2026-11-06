; Compiles the actual shared policy; exits before creating any wizard window.
#ifndef EvidenceDir
  #error EvidenceDir is required
#endif
[Setup]
AppName=MAAZMXY4 installer policy checks
AppVersion=1.0
DefaultDirName={tmp}\unused
CreateAppDir=no
Uninstallable=no
PrivilegesRequired=lowest
OutputDir={#EvidenceDir}
OutputBaseFilename=installer-policy-check

[Code]
#include "..\tools\installer\policy.iss"
var Count: Integer;
procedure Expect(Value: Boolean; const Message: String);
begin
  if not Value then RaiseException(Message);
  Count := Count + 1;
end;
function InitializeSetup: Boolean;
var Dir: String;
begin
  Expect(DestinationFor('C:\') = 'C:\MAAZMXY4', 'drive root');
  Expect(DestinationFor('D:\Games\') = 'D:\Games\MAAZMXY4', 'parent folder');
  Expect(DestinationFor('D:\MAAZMXY4') = 'D:\MAAZMXY4\MAAZMXY4', 'unknown names do not imply identity');
  Expect(VersionAtLeast('10.0.1','10.0.0'), 'matching .NET patch');
  Expect(not VersionAtLeast('9.0.99','10.0.0'), 'older .NET');
  Expect(VersionAtLeast('v14.51.36247.00','14.51.36247.0'), 'VC registry format');
  Expect(VersionAtLeast('14.52.0.0','14.51.36247.0'), 'newer VC is preserved');
  Expect(not VersionAtLeast('14.50.99999.0','14.51.36247.0'), 'older VC');
  Expect(not VersionAtLeast('garbage','10.0.0'), 'invalid version');
  Expect(not VersionAtLeast('10.0.0-preview','10.0.0'), 'preview not stable');
  Expect(DependencyExitOK(0), 'install success');
  Expect(DependencyExitOK(3010), 'success requires restart');
  Expect(not DependencyExitOK(1603), 'install failed');
  Expect(not DependencyExitOK(1223), 'UAC cancelled');
  Expect(not DependencyExitOK(1638), 'other version requires re-detection');
  Dir := ExpandConstant('{tmp}\policy-existing');
  ForceDirectories(Dir+'\agent'); ForceDirectories(Dir+'\runtimes\python');
  SaveStringToFile(Dir+'\MFAAvalonia.exe', '', False);
  SaveStringToFile(Dir+'\agent\main.py', '', False);
  SaveStringToFile(Dir+'\runtimes\python\python.exe', '', False);
  SaveStringToFile(Dir+'\interface.json', UTF8Encode('{"name":"MAA造梦西游4","version":"2.4.3","task":[]}'), False);
  Expect(ExistingClient(Dir), 'identified previous project client');
  Expect(DestinationFor(Dir) = Dir, 'existing client path reused');
  SaveStringToFile(Dir+'\.git', 'gitdir: outside', False);
  Expect(ExistingClient(Dir), 'a runtime client may also have a git worktree marker');
  SaveStringToFile('{#EvidenceDir}\policy-result.txt', 'PASS '+IntToStr(Count), False);
  Result := False;
end;
