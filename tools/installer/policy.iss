// Shared by the actual wizard and its compiled offline policy regression.
function ExistingClient(const Dir: String): Boolean;
var Raw: AnsiString; Text: String;
begin
  Result := False;
  if not (FileExists(AddBackslash(Dir) + 'MFAAvalonia.exe') and
    FileExists(AddBackslash(Dir) + 'agent\main.py') and
    FileExists(AddBackslash(Dir) + 'runtimes\python\python.exe')) then Exit;
  if not LoadStringFromFile(AddBackslash(Dir) + 'interface.json', Raw) then Exit;
  Text := UTF8Decode(Raw);
  StringChangeEx(Text, ' ', '', True); StringChangeEx(Text, #9, '', True);
  StringChangeEx(Text, #13, '', True); StringChangeEx(Text, #10, '', True);
  Result := (Pos('"name":"MAA造梦西游4"', Text) > 0) and (Pos('"version":', Text) > 0);
end;

function DestinationFor(const Selected: String): String;
begin
  Result := RemoveBackslashUnlessRoot(ExpandFileName(Trim(Selected)));
  if not ExistingClient(Result) then Result := AddBackslash(Result) + 'MAAZMXY4';
end;

function VersionAtLeast(const Actual, Required: String): Boolean;
var A, R: TArrayOfString; I, AV, RV: Integer; S: String;
begin
  S := Actual;
  if Copy(S, 1, 1) = 'v' then Delete(S, 1, 1);
  A := StringSplit(S, ['.'], stExcludeEmpty);
  R := StringSplit(Required, ['.'], stExcludeEmpty);
  Result := False;
  if (GetArrayLength(A) < 2) or (GetArrayLength(A) > 4) then Exit;
  for I := 0 to GetArrayLength(A) - 1 do
    if StrToIntDef(A[I], -1) < 0 then Exit;
  for I := 0 to 3 do begin
    AV := 0; RV := 0;
    if I < GetArrayLength(A) then AV := StrToInt(A[I]);
    if I < GetArrayLength(R) then RV := StrToInt(R[I]);
    if AV <> RV then begin Result := AV > RV; Exit; end;
  end;
  Result := True;
end;

function DependencyExitOK(Code: Integer): Boolean;
begin
  Result := (Code = 0) or (Code = 3010);
end;
