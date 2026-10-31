"""Inspect the shipped IL without starting the client or installing input hooks.

FRONTEND_LATENCY_BUILD points to a published frontend. FRONTEND_LATENCY_DOTNET
selects a .NET 10 SDK. The binary check intentionally rejects the old client.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PATCHES = (
    'frontend/patches/55-keyboard-only-hotkeys.json',
    'frontend/patches/56-runtime-managed-memory.json',
)


class FrontendInputLatencyTests(unittest.TestCase):
    def test_optimizations_are_in_the_build_manifest(self):
        manifest = json.loads((ROOT / 'frontend/upstream.json').read_text(encoding='utf8'))
        for path in PATCHES:
            self.assertIn(path, manifest['patches'])
            self.assertTrue((ROOT / path).is_file())

    @unittest.skipUnless(os.environ.get('FRONTEND_LATENCY_BUILD'), 'requires a published frontend')
    def test_published_client_avoids_mouse_hook_and_periodic_memory_cracker(self):
        binary = Path(os.environ['FRONTEND_LATENCY_BUILD']) / 'libs/MFAAvalonia.Core.dll'
        self.assertTrue(binary.is_file(), binary)
        dotnet = os.environ.get('FRONTEND_LATENCY_DOTNET') or shutil.which('dotnet')
        self.assertTrue(dotnet, '.NET 10 SDK is required')
        with tempfile.TemporaryDirectory(prefix='frontend-input-latency-') as temporary:
            folder = Path(temporary)
            (folder / 'Program.cs').write_text(BINARY_CHECK, encoding='utf8')
            (folder / 'Check.csproj').write_text(
                '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                '<TargetFramework>net10.0</TargetFramework><Nullable>enable</Nullable>'
                '<ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>', encoding='utf8')
            result = subprocess.run(
                [dotnet, 'run', '--project', str(folder / 'Check.csproj'), '-c', 'Release', '--', str(binary)],
                capture_output=True, text=True, encoding='utf8', errors='replace', timeout=90,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS keyboard-only hook; keyboard subscription and runner retained', result.stdout)
        self.assertIn('PASS no periodic memory cleaner is constructed or started', result.stdout)


BINARY_CHECK = r'''
using System.Reflection;
using System.Reflection.Emit;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;

using var stream = File.OpenRead(args[0]);
using var pe = new PEReader(stream);
var metadata = pe.GetMetadataReader();
var opcodes = typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static)
    .Select(f => (OpCode)f.GetValue(null)!).ToDictionary(op => unchecked((ushort)op.Value));
var failures = 0;

TypeDefinition Type(string name) => metadata.TypeDefinitions.Select(metadata.GetTypeDefinition)
    .Single(t => metadata.GetString(t.Name) == name);

List<(OpCode Op, int Token)> Instructions(MethodDefinition method)
{
    if (method.RelativeVirtualAddress == 0) return [];
    var bytes = pe.GetMethodBody(method.RelativeVirtualAddress).GetILBytes()!;
    var instructions = new List<(OpCode Op, int Token)>();
    for (var offset = 0; offset < bytes.Length;)
    {
        ushort code = bytes[offset++];
        if (code == 0xfe) code = (ushort)(0xfe00 | bytes[offset++]);
        var op = opcodes[code];
        var length = op.OperandType switch
        {
            OperandType.InlineNone => 0,
            OperandType.ShortInlineI or OperandType.ShortInlineVar or OperandType.ShortInlineBrTarget => 1,
            OperandType.InlineVar => 2,
            OperandType.InlineI8 or OperandType.InlineR => 8,
            OperandType.InlineSwitch => 4 + 4 * BitConverter.ToInt32(bytes, offset),
            _ => 4,
        };
        instructions.Add((op, op.OperandType == OperandType.InlineMethod
            ? BitConverter.ToInt32(bytes, offset) : 0));
        offset += length;
    }
    return instructions;
}

(string Type, string Method) Called(int token)
{
    if (token == 0) return ("", "");
    var handle = MetadataTokens.EntityHandle(token);
    if (handle.Kind == HandleKind.MethodDefinition)
    {
        var method = metadata.GetMethodDefinition((MethodDefinitionHandle)handle);
        return (metadata.GetString(metadata.GetTypeDefinition(method.GetDeclaringType()).Name),
            metadata.GetString(method.Name));
    }
    if (handle.Kind != HandleKind.MemberReference) return ("", "");
    var member = metadata.GetMemberReference((MemberReferenceHandle)handle);
    var type = member.Parent.Kind == HandleKind.TypeReference
        ? metadata.GetString(metadata.GetTypeReference((TypeReferenceHandle)member.Parent).Name) : "";
    return (type, metadata.GetString(member.Name));
}

void Check(bool ok, string message)
{
    Console.WriteLine((ok ? "PASS " : "FAIL ") + message);
    if (!ok) failures++;
}

var hotkey = Type("GlobalHotkeyService").GetMethods().Select(metadata.GetMethodDefinition)
    .Single(m => metadata.GetString(m.Name) == "InitializeAsPrimary");
var hookIl = Instructions(hotkey);
var constructors = hookIl.Select((ins, index) => (ins, index))
    .Where(x => x.ins.Op == OpCodes.Newobj && Called(x.ins.Token) == ("SimpleGlobalHook", ".ctor"))
    .ToArray();
// SharpHook 7.1.1 constructor: (GlobalHookType, provider, runAsyncOnBackgroundThread).
// Keyboard is 1; All is 0. Inspect constants passed by the actual published client.
var keyboardOnly = constructors.Length == 1 && constructors[0].index >= 3
    && hookIl[constructors[0].index - 3].Op == OpCodes.Ldc_I4_1
    && hookIl[constructors[0].index - 2].Op == OpCodes.Ldnull;
Check(keyboardOnly && hookIl.Any(i => Called(i.Token).Method == "add_KeyPressed")
    && hookIl.Any(i => Called(i.Token).Method == "RunAsync"),
    "keyboard-only hook; keyboard subscription and runner retained");

var appCalls = Type("App").GetMethods().Select(metadata.GetMethodDefinition)
    .SelectMany(Instructions).Select(i => Called(i.Token)).ToArray();
Check(!appCalls.Any(c => c.Type == "AvaloniaMemoryCracker" && c.Method is ".ctor" or "Cracker"),
    "no periodic memory cleaner is constructed or started");
return failures == 0 ? 0 : 1;
'''


if __name__ == '__main__':
    unittest.main()
