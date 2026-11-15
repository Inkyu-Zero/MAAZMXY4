using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace MFAAvalonia.Services;

/// <summary>Explicit cleanup of closed .log files, without touching runtime state or user data.</summary>
public sealed class LogCleanup
{
    private static readonly HashSet<string> ProtectedDirectories = new(StringComparer.OrdinalIgnoreCase)
        { "config", "recordings", "drafts", "backup", "backups", "exact_cache", "asset-references", ".git" };
    private readonly string _root;
    private readonly string[] _logRoots;
    private readonly object _operation = new();
    public static bool IsSupported => OperatingSystem.IsWindows();

    public LogCleanup(string dataRoot)
    {
        _root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(dataRoot));
        _logRoots = [Path.Combine(_root, "debug"), Path.Combine(_root, "logs")];
    }

    internal sealed record Entry(string Path, FileIdentity Identity);
    internal sealed class UnsafeLogPathException(string message) : IOException(message) { }
    internal readonly record struct FileIdentity(uint Volume, ulong Index, long Created, long Written, long Bytes);
    public sealed class Plan
    {
        internal LogCleanup Owner { get; }
        internal IReadOnlyList<Entry> Entries { get; }
        public int Files => Entries.Count;
        public long Bytes { get; }
        public int Unavailable { get; }
        internal Plan(LogCleanup owner, List<Entry> entries, long bytes, int unavailable)
        { Owner = owner; Entries = entries.AsReadOnly(); Bytes = bytes; Unavailable = unavailable; }
    }
    public sealed record Result(int Deleted, long Bytes, int Skipped, bool Cancelled);

    public Plan Inspect(CancellationToken token = default)
    {
        RequireSupported();
        var entries = new List<Entry>(); long bytes = 0; var unavailable = 0;
        var pending = new Stack<string>();
        foreach (var root in _logRoots) if (Directory.Exists(root)) pending.Push(root);
        while (pending.TryPop(out var directory))
        {
            token.ThrowIfCancellationRequested();
            try
            {
                RequireOrdinaryDirectories(directory);
                foreach (var path in Directory.EnumerateFileSystemEntries(directory))
                {
                    token.ThrowIfCancellationRequested();
                    try
                    {
                        var attributes = File.GetAttributes(path);
                        if ((attributes & FileAttributes.ReparsePoint) != 0) { unavailable++; continue; }
                        if ((attributes & FileAttributes.Directory) != 0)
                        {
                            if (!ProtectedDirectories.Contains(Path.GetFileName(path))) pending.Push(path);
                        }
                        else if (IsLogPath(path))
                        {
                            // Shared metadata access never reads log contents or interrupts a writer.
                            using var handle = Open(path, delete: false);
                            var identity = Identify(handle, path);
                            entries.Add(new Entry(path, identity)); bytes += identity.Bytes;
                        }
                    }
                    catch (Exception error) when (IsFileError(error)) { unavailable++; }
                }
            }
            catch (Exception error) when (IsFileError(error)) { unavailable++; }
        }
        return new Plan(this, entries, bytes, unavailable);
    }

    public Result Clear(Plan plan, CancellationToken token = default)
    {
        RequireSupported();
        if (!ReferenceEquals(plan.Owner, this)) throw new ArgumentException("日志清理范围已改变，请重新查看。");
        if (!Monitor.TryEnter(_operation)) throw new InvalidOperationException("日志清理正在进行。");
        try
        {
            var deleted = 0; var skipped = 0; long bytes = 0;
            foreach (var entry in plan.Entries)
            {
                if (token.IsCancellationRequested) return new Result(deleted, bytes, skipped, true);
                try
                {
                    if (!DeleteUnchanged(entry, token)) { skipped++; continue; }
                    deleted++; bytes += entry.Identity.Bytes;
                }
                catch (Exception error) when (IsFileError(error)) { skipped++; }
            }
            return new Result(deleted, bytes, skipped, token.IsCancellationRequested);
        }
        finally { Monitor.Exit(_operation); }
    }

    internal bool IsLogPath(string path) => NormalizeLogPath(path) != null;
    internal string? NormalizeLogPath(string path)
    {
        var full = Path.GetFullPath(path);
        if (!string.Equals(Path.GetExtension(full), ".log", StringComparison.OrdinalIgnoreCase)) return null;
        foreach (var root in _logRoots)
            if (full.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            {
                var parts = Path.GetRelativePath(root, full).Split(Path.DirectorySeparatorChar);
                for (var i = 0; i < parts.Length - 1; i++) if (ProtectedDirectories.Contains(parts[i])) return null;
                return full;
            }
        return null;
    }

    internal bool IsLogDirectory(string path)
    {
        var full = Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
        foreach (var root in _logRoots)
            if (string.Equals(full, root, StringComparison.OrdinalIgnoreCase)
                || full.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    internal Entry? ReadEntry(string path)
    {
        var normalized = NormalizeLogPath(path);
        if (normalized == null) return null;
        path = normalized;
        RequireOrdinaryDirectories(Path.GetDirectoryName(path)!);
        using var handle = Open(path, delete: false);
        return new Entry(path, Identify(handle, path));
    }

    internal bool DeleteUnchanged(Entry entry, CancellationToken token)
    {
        if (!IsLogPath(entry.Path)) throw new IOException("文件不属于日志范围。");
        RequireOrdinaryDirectories(Path.GetDirectoryName(entry.Path)!);
        // Hold the exact file identity exclusively through deletion. No contents
        // are read and neither an active writer nor a replacement is truncated.
        using var handle = Open(entry.Path, delete: true);
        if (Identify(handle, entry.Path) != entry.Identity || token.IsCancellationRequested) return false;
        var disposition = new Disposition { DeleteFile = 1 };
        if (!SetFileInformationByHandle(handle, 4, ref disposition, 1)) throw FileError("日志仍在使用或没有删除权限");
        return true;
    }

    private static void RequireOrdinaryDirectories(string directory)
    {
        // Check all ancestors, including a linked installation/log root.
        for (var current = new DirectoryInfo(directory); current != null; current = current.Parent)
            if ((current.Attributes & FileAttributes.ReparsePoint) != 0) throw new UnsafeLogPathException("日志目录包含链接，已跳过。");
    }
    private static bool IsFileError(Exception error) => error is IOException or UnauthorizedAccessException;
    private static void RequireSupported()
    { if (!IsSupported) throw new PlatformNotSupportedException("日志清理目前支持 Windows 客户端。"); }
    private static IOException FileError(string message)
    { var error = Marshal.GetLastWin32Error(); return new(message + "（" + error + "）", new Win32Exception(error)); }

    private static SafeFileHandle Open(string path, bool delete)
    {
        // FILE_READ_ATTRIBUTES (+ DELETE), shared metadata / exclusive cleanup,
        // OPEN_EXISTING, FILE_FLAG_OPEN_REPARSE_POINT. Never follow a file link.
        var handle = CreateFileW(path, 0x80u | (delete ? 0x10000u : 0), delete ? 0u : 7u,
            IntPtr.Zero, 3, 0x00200000, IntPtr.Zero);
        if (!handle.IsInvalid) return handle;
        var error = FileError("无法打开日志"); handle.Dispose(); throw error;
    }

    private static FileIdentity Identify(SafeFileHandle handle, string expectedPath)
    {
        if (!GetFileInformationByHandle(handle, out var data)) throw FileError("无法读取日志信息");
        if ((data.Attributes & (uint)(FileAttributes.ReparsePoint | FileAttributes.Directory)) != 0 || data.Links != 1)
            throw new UnsafeLogPathException("链接或目录不属于可清理日志。");
        var path = new StringBuilder(512);
        var size = GetFinalPathNameByHandleW(handle, path, (uint)path.Capacity, 0);
        if (size >= path.Capacity && size < 32768)
        { path = new StringBuilder((int)size + 1); size = GetFinalPathNameByHandleW(handle, path, (uint)path.Capacity, 0); }
        if (size == 0 || size >= path.Capacity) throw FileError("无法核对日志路径");
        var final = path.ToString();
        if (final.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) final = @"\\" + final[8..];
        else if (final.StartsWith(@"\\?\", StringComparison.Ordinal)) final = final[4..];
        if (!string.Equals(Path.GetFullPath(final), Path.GetFullPath(expectedPath), StringComparison.OrdinalIgnoreCase))
            throw new UnsafeLogPathException("日志实际路径已改变。");
        return new FileIdentity(data.Volume, ((ulong)data.IndexHigh << 32) | data.IndexLow,
            ((long)data.Created.High << 32) | data.Created.Low, ((long)data.Written.High << 32) | data.Written.Low,
            checked((long)(((ulong)data.SizeHigh << 32) | data.SizeLow)));
    }

    [StructLayout(LayoutKind.Sequential)] private struct FileTime { public uint Low, High; }
    [StructLayout(LayoutKind.Sequential)] private struct FileInfo
    {
        public uint Attributes;
        public FileTime Created, Accessed, Written;
        public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [StructLayout(LayoutKind.Sequential)] private struct Disposition { public byte DeleteFile; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle file, out FileInfo info);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandleW(SafeFileHandle file, StringBuilder path, uint capacity, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)] [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle file, int infoClass, ref Disposition info, uint size);
}
