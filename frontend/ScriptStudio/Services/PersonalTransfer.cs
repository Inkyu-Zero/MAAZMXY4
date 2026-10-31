using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Security.Cryptography;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace MFAAvalonia.Services;

/// <summary>Portable personal data only. Never extracts archive-supplied paths directly.</summary>
public sealed class PersonalTransfer(string root, string? configDirectory = null)
{
    public const long MaxFile = 64 * 1024 * 1024, MaxTotal = 512 * 1024 * 1024;
    public const int MaxFiles = 10000;
    private readonly string _root = Path.GetFullPath(root);
    private readonly string _config = Path.GetFullPath(configDirectory ?? Path.Combine(root, "config"));
    private string State => Safe(_root, ".personal-transfer");
    private string Pending => Safe(State, "pending");
    public sealed record Item(string Path, long Size, string Sha256);
    public sealed record Manifest(int Schema, string Product, string Created, string ResourceVersion, Item[] Files);
    public sealed record Preview(string Created, string ResourceVersion, int Files, int Overwrites, long Bytes);
    public bool HasPending => Directory.Exists(Pending);
    public string LastResult => File.Exists(Safe(State, "result.txt")) ? File.ReadAllText(Safe(State, "result.txt")) : "";
    private static string Hash(byte[] value) => Convert.ToHexString(SHA256.HashData(value));
    private static void Write(string path, byte[] bytes)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try { using (var stream = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None)) { stream.Write(bytes); stream.Flush(true); } File.Move(temp, path, true); }
        finally { if (File.Exists(temp)) File.Delete(temp); }
    }
    public static string Safe(string root, string relative)
    {
        var parts = relative.Replace('\\', '/').Split('/');
        if (parts.Any(p => string.IsNullOrWhiteSpace(p) || p is "." or ".." || p.EndsWith('.') || p.EndsWith(' ') || p.IndexOfAny("<>:\"|?*\0".ToCharArray()) >= 0 || p.Any(char.IsControl) ||
            new[] { "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9" }.Contains(p.Split('.')[0], StringComparer.OrdinalIgnoreCase)))
            throw new InvalidDataException("备份中存在无效路径。");
        var full = Path.GetFullPath(Path.Combine(root, Path.Combine(parts)));
        var boundary = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!full.StartsWith(boundary, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("备份路径越界。");
        for (var p = new FileInfo(full) as FileSystemInfo; p != null; p = Directory.GetParent(p.FullName))
            if ((File.Exists(p.FullName) || Directory.Exists(p.FullName)) && (File.GetAttributes(p.FullName) & FileAttributes.ReparsePoint) != 0)
                throw new InvalidDataException("个人数据路径不能包含链接。");
        return full;
    }
    private static bool Allowed(string path) => path == "appsettings.json" ||
        ((path.StartsWith("config/", StringComparison.Ordinal) || path.StartsWith("recordings/", StringComparison.Ordinal)) && path.EndsWith(".json", StringComparison.OrdinalIgnoreCase)) ||
        (path.StartsWith("resource/", StringComparison.Ordinal) && path.EndsWith("/mfa_layout.json", StringComparison.Ordinal));
    private string Destination(string name) => name.StartsWith("config/", StringComparison.Ordinal)
        ? Safe(_config, name[7..]) : name == "appsettings.json"
        ? Safe(Path.GetDirectoryName(_config)!, name) : Safe(_root, name);
    private IEnumerable<string> Names()
    {
        foreach (var folder in new[] { "config", "recordings", "resource" })
        {
            var path = folder == "config" ? _config : Safe(_root, folder);
            if (!Directory.Exists(path)) continue;
            // Do not let recursive enumeration follow junctions.
            var queue = new Queue<string>(); queue.Enqueue(path);
            while (queue.Count > 0)
                foreach (var entry in Directory.EnumerateFileSystemEntries(queue.Dequeue()))
                {
                    var relative = folder + "/" + Path.GetRelativePath(path, entry).Replace('\\', '/');
                    _ = Safe(path, Path.GetRelativePath(path, entry));
                    if (Directory.Exists(entry)) queue.Enqueue(entry);
                    else if (Allowed(relative)) yield return relative;
                }
        }
        if (File.Exists(Destination("appsettings.json"))) yield return "appsettings.json";
    }
    private static byte[] Read(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        if (stream.Length > MaxFile) throw new InvalidDataException("单个配置或脚本超过 64 MB。");
        using var memory = new MemoryStream(); stream.CopyTo(memory); return memory.ToArray();
    }
    private static void Json(byte[] value) { using var doc = JsonDocument.Parse(value.AsMemory(value.AsSpan().StartsWith(new byte[] { 239, 187, 191 }) ? 3 : 0)); }
    public async Task ExportAsync(string archive, CancellationToken token = default)
    {
        using var gate = Lock();
        if (!archive.EndsWith(".zip", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("请选择 .zip 文件名。");
        archive = Path.GetFullPath(archive);
        foreach (var directory in new[] { _config, Safe(_root, "recordings"), State })
            if (archive.StartsWith(directory.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("请把备份保存在个人数据目录之外。");
        var temp = Path.Combine(Path.GetTempPath(), "maazmxy4-export-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(temp);
        var output = Path.Combine(Path.GetDirectoryName(archive)!, "." + Guid.NewGuid().ToString("N") + ".zip");
        try
        {
            var names = Names().Order(StringComparer.Ordinal).ToArray();
            if (names.Length == 0 || names.Length > MaxFiles) throw new InvalidDataException("没有已保存的个人数据，或文件数量过多。");
            var items = new List<Item>(); long total = 0;
            foreach (var name in names)
            {
                token.ThrowIfCancellationRequested(); var data = Read(Destination(name)); Json(data);
                if ((total += data.Length) > MaxTotal) throw new InvalidDataException("个人数据超过 512 MB。");
                Write(Safe(temp, name), data); items.Add(new(name, data.Length, Hash(data)));
            }
            if (!names.SequenceEqual(Names().Order(StringComparer.Ordinal)) || items.Any(i => Hash(Read(Destination(i.Path))) != i.Sha256)) throw new IOException("个人数据正在变化，请停止编辑后重新导出。");
            var version = ""; var interfacePath = Safe(_root, "interface.json");
            if (File.Exists(interfacePath)) { using var doc = JsonDocument.Parse(Read(interfacePath)); version = doc.RootElement.TryGetProperty("version", out var v) ? v.GetString() ?? "" : ""; }
            Write(Safe(temp, "manifest.json"), JsonSerializer.SerializeToUtf8Bytes(new Manifest(1, "MAAZMXY4-personal", DateTimeOffset.Now.ToString("O"), version, items.ToArray())));
            // Compression/decompression are part of the application's .NET
            // runtime. No external executable, registry lookup or download.
            using (var file = new FileStream(output, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None))
            {
                using (var zip = new ZipArchive(file, ZipArchiveMode.Create, leaveOpen: true, entryNameEncoding: System.Text.Encoding.UTF8))
                    foreach (var inputPath in Directory.EnumerateFiles(temp, "*", SearchOption.AllDirectories))
                    {
                        token.ThrowIfCancellationRequested();
                        var entry = zip.CreateEntry(Path.GetRelativePath(temp, inputPath).Replace('\\', '/'), CompressionLevel.Optimal);
                        using var input = File.OpenRead(inputPath); using var compressed = entry.Open();
                        await input.CopyToAsync(compressed, token);
                    }
                file.Flush(true);
            }
            _ = Inspect(output); token.ThrowIfCancellationRequested(); File.Move(output, archive, true);
        }
        finally { if (File.Exists(output)) File.Delete(output); Directory.Delete(temp, true); }
    }
    private static Dictionary<string, byte[]> Unpack(string path)
    {
        using var file = File.OpenRead(path);
        if (file.Length > MaxTotal + 16 * 1024 * 1024) throw new InvalidDataException("备份包过大。");
        Span<byte> signature = stackalloc byte[4]; if (file.Read(signature) != 4 || !signature.SequenceEqual(new byte[] { 0x50, 0x4b, 0x03, 0x04 })) throw new InvalidDataException("请选择由助手导出的 ZIP 个人备份，不能直接修改其它格式的扩展名。");
        file.Position = 0; using var archive = new ZipArchive(file, ZipArchiveMode.Read);
        var data = new Dictionary<string, byte[]>(StringComparer.OrdinalIgnoreCase); long total = 0;
        var entries = 0;
        foreach (var entry in archive.Entries)
        {
            if (++entries > MaxFiles * 2) throw new InvalidDataException("备份条目过多。");
            var name = entry.FullName.Replace('\\', '/').TrimEnd('/'); _ = Safe(Path.GetTempPath(), name);
            if (entry.IsEncrypted) throw new InvalidDataException("不支持加密的 ZIP 备份。");
            var unixType = (entry.ExternalAttributes >> 16) & 0xf000;
            if (unixType == 0xa000 || (entry.ExternalAttributes & (int)FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("不支持链接备份。");
            if (entry.FullName.EndsWith('/') || entry.FullName.EndsWith('\\')) continue;
            if ((name != "manifest.json" && !Allowed(name)) || entry.Length < 0 || entry.Length > MaxFile || (total += entry.Length) > MaxTotal || data.Count >= MaxFiles + 1 || data.ContainsKey(name)) throw new InvalidDataException("备份包含不支持的文件、重复路径或超出大小限制。");
            using var input = entry.Open(); using var memory = new MemoryStream(); var buffer = new byte[65536]; int read;
            while ((read = input.Read(buffer)) != 0) { if (memory.Length + read > entry.Length) throw new InvalidDataException("备份文件大小不符。"); memory.Write(buffer, 0, read); }
            if (memory.Length != entry.Length) throw new InvalidDataException("备份不完整。"); data.Add(name, memory.ToArray());
        }
        Validate(data); return data;
    }
    private static Manifest Validate(Dictionary<string, byte[]> data)
    {
        if (!data.TryGetValue("manifest.json", out var raw)) throw new InvalidDataException("备份缺少清单。");
        var manifest = JsonSerializer.Deserialize<Manifest>(raw) ?? throw new InvalidDataException("备份清单无效。");
        if (manifest.Schema != 1 || manifest.Product != "MAAZMXY4-personal" || manifest.Files == null || manifest.Files.Length == 0 || manifest.Files.Length > MaxFiles || data.Count != manifest.Files.Length + 1) throw new InvalidDataException("备份版本或文件清单不受支持。");
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var item in manifest.Files)
        {
            _ = Safe(Path.GetTempPath(), item.Path);
            if (!Allowed(item.Path) || !names.Add(item.Path) || !data.TryGetValue(item.Path, out var bytes) || bytes.LongLength != item.Size || Hash(bytes) != item.Sha256) throw new InvalidDataException("备份校验失败，文件缺失或已被修改。");
            Json(bytes);
        }
        return manifest;
    }
    public Preview Inspect(string archive)
    {
        var data = Unpack(archive); var manifest = Validate(data);
        return new(manifest.Created, manifest.ResourceVersion, manifest.Files.Length, manifest.Files.Count(i => File.Exists(Destination(i.Path))), manifest.Files.Sum(i => i.Size));
    }
    public void Stage(string archive, string expectedArchiveHash)
    {
        using var gate = Lock(); if (HasPending) throw new IOException("已有待导入备份，请先取消或重启完成导入。");
        var staging = Safe(State, "stage-" + Guid.NewGuid().ToString("N")); Directory.CreateDirectory(staging);
        try
        {
            var copy = Safe(staging, "bundle.zip"); File.Copy(archive, copy);
            if (ArchiveHash(copy) != expectedArchiveHash) throw new IOException("备份在预览后发生变化，请重新选择。");
            var files = Unpack(copy); File.Delete(copy);
            foreach (var item in files) Write(Safe(staging, item.Key), item.Value);
            Directory.Move(staging, Pending);
        }
        finally { if (Directory.Exists(staging)) Directory.Delete(staging, true); }
    }
    public static string ArchiveHash(string path) { using var input = File.OpenRead(path); if (input.Length > MaxTotal + 16 * 1024 * 1024) throw new InvalidDataException("备份包过大。"); return Convert.ToHexString(SHA256.HashData(input)); }
    private FileStream Lock() { Directory.CreateDirectory(State); return new FileStream(Safe(State, "transfer.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
    public void CancelPending() { using var gate = Lock(); if (HasPending) Directory.Delete(Pending, true); }
    public sealed record Journal(string Backup, Item[] Files, string[] Existed);
    public static void ApplyOnStartup(string root, string configDirectory)
    {
        var transfer = new PersonalTransfer(root, configDirectory);
        if (!transfer.HasPending && !File.Exists(Safe(transfer.State, "journal.json"))) return;
        using var current = Process.GetCurrentProcess();
        foreach (var other in Process.GetProcessesByName(current.ProcessName))
            using (other)
            {
                if (other.Id == current.Id) continue;
                bool same;
                try { same = string.Equals(other.MainModule?.FileName, Environment.ProcessPath, StringComparison.OrdinalIgnoreCase); }
                catch (System.ComponentModel.Win32Exception) { same = true; }
                catch (InvalidOperationException) { continue; }
                if (same) throw new IOException("还有另一个助手实例正在运行。请关闭同一目录的所有助手后重新启动，以安全导入个人备份。");
            }
        transfer.ApplyPending();
    }
    // Called before the host reads any personal configuration. The durable
    // journal survives process termination; the next launch rolls back first.
    public void ApplyPending(Action<int>? afterWrite = null)
    {
        if (!HasPending && !File.Exists(Safe(State, "journal.json"))) return;
        using var gate = Lock(); var journalPath = Safe(State, "journal.json");
        void Quarantine(string message)
        {
            if (HasPending) Directory.Move(Pending, Safe(State, "rejected-" + Guid.NewGuid().ToString("N")));
            Write(Safe(State, "result.txt"), System.Text.Encoding.UTF8.GetBytes(message));
        }
        void Rollback(Journal journal)
        {
            if (!journal.Backup.StartsWith("backup-", StringComparison.Ordinal) || journal.Backup.Contains('/') || journal.Backup.Contains('\\') || journal.Files.Length > MaxFiles || journal.Files.Any(i => !Allowed(i.Path))) throw new InvalidDataException("导入恢复记录损坏。");
            foreach (var item in journal.Files)
            {
                var dest = Destination(item.Path);
                if (journal.Existed.Contains(item.Path)) Write(dest, Read(Safe(Safe(State, journal.Backup), item.Path)));
                else if (File.Exists(dest)) File.Delete(dest);
            }
            File.Delete(journalPath);
        }
        if (File.Exists(journalPath))
        {
            Rollback(JsonSerializer.Deserialize<Journal>(Read(journalPath)) ?? throw new InvalidDataException("导入恢复记录损坏。"));
            Quarantine("上次导入未完成，已恢复原配置。未完成的备份已保留在 .personal-transfer/rejected-*，请在版本页面重新选择备份。");
            return;
        }
        if (!HasPending) return;
        var data = new Dictionary<string, byte[]>(StringComparer.OrdinalIgnoreCase);
        Manifest manifest;
        try
        {
            var raw = Read(Safe(Pending, "manifest.json")); data.Add("manifest.json", raw);
            var declared = JsonSerializer.Deserialize<Manifest>(raw);
            if (declared?.Files == null || declared.Files.Length > MaxFiles) throw new InvalidDataException("备份清单无效。");
            long total = 0;
            foreach (var item in declared.Files)
            {
                if (!Allowed(item.Path) || (total += item.Size) > MaxTotal || item.Size < 0 || item.Size > MaxFile) throw new InvalidDataException("备份条目无效。");
                data.Add(item.Path, Read(Safe(Pending, item.Path)));
            }
            manifest = Validate(data);
        }
        catch (Exception error) { Quarantine("导入未执行，当前配置未改变：" + error.Message); return; }
        var backup = "backup-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N");
        var existed = new List<string>();
        foreach (var item in manifest.Files)
            if (File.Exists(Destination(item.Path))) { Write(Safe(Safe(State, backup), item.Path), Read(Destination(item.Path))); existed.Add(item.Path); }
        var journal = new Journal(backup, manifest.Files, existed.ToArray()); Write(journalPath, JsonSerializer.SerializeToUtf8Bytes(journal));
        try
        {
            var count = 0; foreach (var item in manifest.Files) { Write(Destination(item.Path), data[item.Path]); afterWrite?.Invoke(++count); }
            // Removing pending before the journal is safe: interrupted cleanup
            // rolls back instead of silently treating a partial import as done.
            Directory.Delete(Pending, true); File.Delete(journalPath);
            Write(Safe(State, "result.txt"), System.Text.Encoding.UTF8.GetBytes($"已导入 {manifest.Files.Length} 个文件。原配置备份：{Path.Combine(State, backup)}"));
        }
        catch (Exception error) { Rollback(journal); Quarantine("导入失败，已恢复原配置：" + error.Message); }
    }
}
