using MFAAvalonia.Services;
using System.IO.Compression;
using System.Diagnostics;
using System.Text.Json;

try { await Run(args); }
catch (Exception error) { Console.Error.WriteLine(error); Environment.ExitCode = 1; }
static async Task Run(string[] args)
{
if(args.Length > 0 && args[0]=="interrupt") { new PersonalTransfer(args[1]).ApplyPending(_=>Environment.Exit(73)); return; }
var temp = Path.Combine(Path.GetTempPath(),"personal-transfer-tests-"+Guid.NewGuid().ToString("N"));
Directory.CreateDirectory(temp); int passed=0;
void Check(bool value,string name){if(!value)throw new Exception(name);passed++;Console.WriteLine("PASS "+name);}
void Write(string root,string name,string value){var path=Path.Combine(root,name);Directory.CreateDirectory(Path.GetDirectoryName(path)!);File.WriteAllText(path,value);}
async Task Reject(Func<Task> action,string name){try{await action();}catch(Exception e) when(e is IOException or InvalidDataException or System.Text.Json.JsonException or InvalidOperationException or OperationCanceledException){Check(true,name);return;}throw new Exception("Accepted "+name);}
try
{
    var source=Path.Combine(temp,"旧助手 中文");var target=Path.Combine(temp,"新助手");Directory.CreateDirectory(source);Directory.CreateDirectory(target);
    Write(source,"config/instances/default.json","{\"count\":7,\"afk\":true,\"selected\":true}");
    Write(source,"recordings/我的脚本.json","{\"version\":1,\"actions\":[]}");
    Write(source,"recordings/.bindings.json","{\"task\":\"我的脚本.json\"}");
    Write(source,"recordings/.defaults.json","{\"deleted\":[\"old\"]}");
    Write(source,"appsettings.json","{\"theme\":\"Dark\"}");
    Write(source,"resource/base/mfa_layout.json","{\"width\":123}");
    Write(source,"agent/main.py","private code not exported");Write(source,"logs/private.json","{}");Write(source,"interface.json","{\"version\":\"2.4.2\"}");
    var export=new PersonalTransfer(source);var archive=Path.Combine(temp,"个人备份.zip");await export.ExportAsync(archive);
    if(args.Length==1)File.Copy(archive,Path.GetFullPath(args[0]),true);
    Check(File.ReadAllBytes(archive).Take(4).SequenceEqual(new byte[]{80,75,3,4}),"real ZIP signature without external tools");
    var import=new PersonalTransfer(target);var preview=import.Inspect(archive);Check(preview.Files==6,"allowlist includes bindings/defaults/layout; excludes runtime/logs");
    Write(target,"config/instances/default.json","{\"count\":3}");Write(target,"recordings/unrelated.json","{}");
    import.Stage(archive,PersonalTransfer.ArchiveHash(archive));Check(File.ReadAllText(Path.Combine(target,"config/instances/default.json")).Contains('3'),"staging does not overwrite live settings");
    import.ApplyPending();Check(!import.HasPending,"startup consumes staged import");
    foreach(var name in new[]{"config/instances/default.json","recordings/我的脚本.json","recordings/.bindings.json","recordings/.defaults.json","appsettings.json","resource/base/mfa_layout.json"})Check(File.ReadAllBytes(Path.Combine(source,name)).SequenceEqual(File.ReadAllBytes(Path.Combine(target,name))),"round trip "+name);
    Check(File.Exists(Path.Combine(target,"recordings/unrelated.json")),"unrelated files retained");
    Check(Directory.EnumerateFiles(Path.Combine(target,".personal-transfer"),"default.json",SearchOption.AllDirectories).Any(p=>File.ReadAllText(p).Contains('3')),"original configuration backed up");
    import.Stage(archive,PersonalTransfer.ArchiveHash(archive));import.CancelPending();Check(!import.HasPending,"cancel pending import");
    await Reject(()=>{import.Stage(archive,"bad hash");return Task.CompletedTask;},"preview/archive change rejected");
    var fake=Path.Combine(temp,"fake.zip");File.WriteAllBytes(fake,new byte[]{82,97,114,33,26,7,1,0});await Reject(()=>Task.Run(()=>import.Inspect(fake)),"renamed non-ZIP rejected");
    foreach(var path in new[]{"../x.json","/config/a.json","config/a.json:ads","config/CON.json","config/../x","config/x. /a.json"})
        await Reject(()=>Task.Run(()=>PersonalTransfer.Safe(target,path)),"path rejected "+path);
    Write(target,"config/instances/default.json","{\"count\":88}");
    import.Stage(archive,PersonalTransfer.ArchiveHash(archive));import.ApplyPending(_=>throw new IOException("injected write failure"));
    Check(!import.HasPending && import.LastResult.Contains("已恢复原配置"),"partial write failure rolls back and does not prevent app startup");
    Check(File.ReadAllText(Path.Combine(target,"config/instances/default.json")).Contains("88"),"rollback preserves original bytes");
    import.CancelPending();
    import.Stage(archive,PersonalTransfer.ArchiveHash(archive));
    var start=new ProcessStartInfo(Environment.ProcessPath!){UseShellExecute=false,CreateNoWindow=true};start.ArgumentList.Add("interrupt");start.ArgumentList.Add(target);
    using(var child=Process.Start(start)!){await child.WaitForExitAsync();Check(child.ExitCode==73,"simulated process termination during import");}
    Check(File.Exists(Path.Combine(target,".personal-transfer/journal.json")),"recovery journal survives process termination");
    import.ApplyPending();Check(File.ReadAllText(Path.Combine(target,"config/instances/default.json")).Contains("88") && !import.HasPending,"next launch rolls back interrupted import without automatic retry");
    import.Stage(archive,PersonalTransfer.ArchiveHash(archive));
    Write(target,".personal-transfer/pending/recordings/我的脚本.json","{\"tampered\":true}");
    import.ApplyPending();Check(import.LastResult.Contains("未改变") && !import.HasPending,"tampered staged payload rejected before writes");
    var changed=Path.Combine(temp,"changed");Directory.CreateDirectory(changed);
    using(var original=ZipFile.OpenRead(archive))
        foreach(var entry in original.Entries.Where(e=>!e.FullName.EndsWith('/')))
        {var dest=PersonalTransfer.Safe(changed,entry.FullName);Directory.CreateDirectory(Path.GetDirectoryName(dest)!);using var input=entry.Open();using var output=File.Create(dest);input.CopyTo(output);}
    async Task PackInvalid(string name)
    {
        var invalid=Path.Combine(temp,name+".zip");ZipFile.CreateFromDirectory(changed,invalid);
        await Reject(()=>Task.Run(()=>import.Inspect(invalid)),name+" rejected");
    }
    Write(changed,"recordings/我的脚本.json","{}");await PackInvalid("checksum-mismatch");
    File.Copy(Path.Combine(source,"recordings/我的脚本.json"),Path.Combine(changed,"recordings/我的脚本.json"),true);
    Write(changed,"agent/execute.py","unexpected program");await PackInvalid("unexpected-runtime-file");
    File.Delete(Path.Combine(changed,"agent/execute.py"));
    var manifestPath=Path.Combine(changed,"manifest.json");var parsed=JsonSerializer.Deserialize<PersonalTransfer.Manifest>(File.ReadAllText(manifestPath))!;
    File.WriteAllText(manifestPath,JsonSerializer.Serialize(parsed with{Schema=999}));await PackInvalid("unsupported-schema");
    File.WriteAllText(manifestPath,JsonSerializer.Serialize(parsed with{Files=parsed.Files.Select((v,i)=>i==0?v with{Path="../outside.json"}:v).ToArray()}));await PackInvalid("manifest-traversal");
    async Task AlterZip(string name, Action<ZipArchive> alter)
    {
        var path=Path.Combine(temp,name+".zip");File.Copy(archive,path);
        using(var zip=ZipFile.Open(path,ZipArchiveMode.Update))alter(zip);
        await Reject(()=>Task.Run(()=>import.Inspect(path)),name+" rejected");
    }
    await AlterZip("duplicate-entry",zip=>zip.CreateEntry("recordings/.bindings.json"));
    await AlterZip("case-alias",zip=>zip.CreateEntry("recordings/.BINDINGS.json"));
    await AlterZip("separator-alias",zip=>zip.CreateEntry("recordings\\.bindings.json"));
    await AlterZip("directory-traversal",zip=>zip.CreateEntry("../../outside/"));
    await AlterZip("unix-symlink",zip=>zip.GetEntry("recordings/我的脚本.json")!.ExternalAttributes=unchecked((int)0xa1ff0000));
    await AlterZip("windows-link",zip=>zip.GetEntry("recordings/我的脚本.json")!.ExternalAttributes=(int)FileAttributes.ReparsePoint);
    var encrypted=Path.Combine(temp,"encrypted.zip");var marked=File.ReadAllBytes(archive);
    for(var i=0;i<marked.Length-46;i++)
    {
        if(marked.AsSpan(i,4).SequenceEqual(new byte[]{80,75,3,4}))marked[i+6]|=1;
        if(marked.AsSpan(i,4).SequenceEqual(new byte[]{80,75,1,2}))marked[i+8]|=1;
    }
    File.WriteAllBytes(encrypted,marked);await Reject(()=>Task.Run(()=>import.Inspect(encrypted)),"encrypted archive rejected");
    var oversized=Path.Combine(temp,"oversized.zip");var forged=File.ReadAllBytes(archive);var central=forged.AsSpan().IndexOf(new byte[]{80,75,1,2});
    BitConverter.GetBytes((uint)PersonalTransfer.MaxFile+1).CopyTo(forged,central+24);File.WriteAllBytes(oversized,forged);
    await Reject(()=>Task.Run(()=>import.Inspect(oversized)),"oversized entry rejected before decompression");
    var truncated=Path.Combine(temp,"truncated.zip");File.WriteAllBytes(truncated,File.ReadAllBytes(archive)[..40]);
    await Reject(()=>Task.Run(()=>import.Inspect(truncated)),"truncated ZIP rejected");
    var archiveHash=PersonalTransfer.ArchiveHash(archive);
    using(var cancelled=new CancellationTokenSource())
    {cancelled.Cancel();await Reject(()=>export.ExportAsync(archive,cancelled.Token),"cancelled export preserves previous archive");}
    Check(PersonalTransfer.ArchiveHash(archive)==archiveHash,"cancelled export leaves prior ZIP byte-identical");
    var custom=Path.Combine(temp,"custom config");Directory.CreateDirectory(custom);var alternate=new PersonalTransfer(target,custom);
    alternate.Stage(archive,PersonalTransfer.ArchiveHash(archive));alternate.ApplyPending();Check(File.Exists(Path.Combine(custom,"instances/default.json")),"active alternate config directory supported");
    Console.WriteLine($"{passed} checks passed; no app, controller, capture or input device was started.");
}
finally { Directory.Delete(temp,true); }

}
