using System.Reflection;
using System.Reflection.Emit;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;

internal static class StartupContract
{
    public static void Check()
    {
        using var file=File.OpenRead(Path.Combine(AppContext.BaseDirectory,"DesktopToInspect.dll"));using var pe=new PEReader(file);var meta=pe.GetMetadataReader();
        var program=meta.TypeDefinitions.Select(meta.GetTypeDefinition).Single(t=>meta.GetString(t.Name)=="Program");
        var main=program.GetMethods().Select(meta.GetMethodDefinition).Single(m=>meta.GetString(m.Name)=="Main");
        var opcodes=typeof(OpCodes).GetFields(BindingFlags.Static|BindingFlags.Public).Select(f=>(OpCode)f.GetValue(null)!).ToDictionary(o=>unchecked((ushort)o.Value));
        var bytes=pe.GetMethodBody(main.RelativeVirtualAddress).GetILBytes()!;var calls=new List<(string Type,string Method)>();
        for(var offset=0;offset<bytes.Length;)
        {
            ushort code=bytes[offset++];if(code==0xfe)code=(ushort)(0xfe00|bytes[offset++]);var op=opcodes[code];
            var size=op.OperandType switch {OperandType.InlineNone=>0,OperandType.ShortInlineI or OperandType.ShortInlineVar or OperandType.ShortInlineBrTarget=>1,OperandType.InlineVar=>2,OperandType.InlineI8 or OperandType.InlineR=>8,OperandType.InlineSwitch=>4+4*BitConverter.ToInt32(bytes,offset),_=>4};
            if(op.OperandType==OperandType.InlineMethod)
            {
                var handle=MetadataTokens.EntityHandle(BitConverter.ToInt32(bytes,offset));
                if(handle.Kind==HandleKind.MemberReference)
                {
                    var member=meta.GetMemberReference((MemberReferenceHandle)handle);
                    var type=member.Parent.Kind==HandleKind.TypeReference?meta.GetString(meta.GetTypeReference((TypeReferenceHandle)member.Parent).Name):"";
                    calls.Add((type,meta.GetString(member.Name)));
                }
            }
            offset+=size;
        }
        var restore=calls.IndexOf(("PersonalTransfer","ApplyOnStartup"));
        foreach(var later in new[]{("AppRuntime","Initialize"),("TelemetryService","InitializeBootstrapFromInterface"),("LoggerHelper","InitializeLogger")})
            if(restore<0||calls.IndexOf(later)<=restore)throw new Exception("Restore must precede settings initialization: "+later);
        if(calls.Take(restore).Any(c=>c.Type=="AppPaths"&&c.Method=="Initialize"))throw new Exception("AppPaths logging may initialize settings before restore");
        Console.WriteLine("PASS actual desktop IL restores before runtime, telemetry and logger initialize cached settings.");
    }
}
