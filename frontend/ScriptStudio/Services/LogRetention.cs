using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using MFAAvalonia.Configuration;

namespace MFAAvalonia.Services;

/// <summary>One host-owned, event-driven byte budget shared by all log writers.</summary>
public sealed class LogRetention : IDisposable
{
    public const long WindowBytes = 64L * 1024 * 1024;
    public const long BlockBytes = 4L * 1024 * 1024;
    private static LogRetention? _current;
    private static readonly object Lifetime = new();
    private static Task _stopping = Task.CompletedTask;
    private static string? _root;
    private static bool _shutdown;
    private static int _enabled = 1;
    public static bool Enabled => Volatile.Read(ref _enabled) != 0;
    public static event Action? SettingChanged;
    private readonly LogCleanup _files;
    private readonly long _limit;
    private readonly ConcurrentDictionary<string, byte> _dirty = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, LogCleanup.Entry> _index = new(StringComparer.OrdinalIgnoreCase);
    private readonly SortedSet<LogCleanup.Entry> _oldest = new(Comparer<LogCleanup.Entry>.Create((a, b) =>
    {
        var time = a.Identity.Written.CompareTo(b.Identity.Written);
        return time != 0 ? time : StringComparer.OrdinalIgnoreCase.Compare(a.Path, b.Path);
    }));
    private readonly Dictionary<string, long> _retryAfter = new(StringComparer.OrdinalIgnoreCase);
    private readonly SemaphoreSlim _wake = new(0, 1);
    private readonly CancellationTokenSource _stop = new();
    private readonly FileSystemWatcher[] _watchers;
    private readonly Task _worker;
    private int _signalled, _rescan = 1, _disposed;
    private long _bytes, _scans, _reads, _notifications, _deleted;
    private Status _status = new(0, 0, 0, 0, 0, 0, "");
    public sealed record Status(long Bytes, int Files, long FullScans, long MetadataReads, long Notifications, long Deleted, string Error);
    public Status Snapshot => Volatile.Read(ref _status);
    public Task Completion => _worker;

    public static void Start(string root)
    {
        if (!LogCleanup.IsSupported) return;
        lock (Lifetime)
        {
            if (_current != null) return;
            _root = Path.GetFullPath(root); _shutdown = false;
            try
            {
                Volatile.Write(ref _enabled, GlobalConfiguration.GetLogRetentionEnabled() ? 1 : 0);
                if (Enabled) StartCurrent();
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or System.Text.Json.JsonException)
            { System.Diagnostics.Debug.WriteLine("日志滚动未启动：" + error.Message); }
        }
    }
    public static void Stop()
    { lock (Lifetime) { _shutdown = true; StopCurrent(); } }

    public static Task SetEnabledAsync(bool enabled) => Task.Run(() =>
    {
        try
        {
            lock (Lifetime)
            {
                if (_shutdown || _root == null) throw new InvalidOperationException("日志服务尚未启动或正在退出。");
                if (enabled == Enabled) return;
                // Persistence failure leaves the running service unchanged.
                GlobalConfiguration.SetLogRetentionEnabled(enabled);
                Volatile.Write(ref _enabled, enabled ? 1 : 0);
                if (enabled) StartCurrent();
                else
                {
                    StopCurrent();
                    // On a worker thread: drain the old owner's last operation
                    // before reporting disabled or allowing another enable.
                    _stopping.GetAwaiter().GetResult();
                }
            }
        }
        finally { SettingChanged?.Invoke(); }
    });

    private static void StartCurrent()
    {
        _stopping.GetAwaiter().GetResult();
        _current ??= new LogRetention(_root!);
    }
    private static void StopCurrent()
    {
        if (_current == null) return;
        _current.Dispose(); _stopping = _current.Completion; _current = null;
    }

    public LogRetention(string root, long limit = WindowBytes, bool watch = true)
    {
        if (!LogCleanup.IsSupported) throw new PlatformNotSupportedException();
        if (limit <= 0) throw new ArgumentOutOfRangeException(nameof(limit));
        root = Path.GetFullPath(root); _files = new LogCleanup(root); _limit = limit;
        var watchers = new List<FileSystemWatcher>();
        try
        {
            if (watch)
            {
                // Watching the data root also handles debug/logs being created
                // later. Path filtering happens in memory before any file IO.
                var logs = new FileSystemWatcher(root, "*.log")
                { IncludeSubdirectories = true, NotifyFilter = NotifyFilters.FileName | NotifyFilters.Size | NotifyFilters.LastWrite, InternalBufferSize = 16384 };
                watchers.Add(logs);
                logs.Changed += (_, e) => NotifyFile(e.FullPath);
                logs.Created += (_, e) => NotifyFile(e.FullPath);
                logs.Deleted += (_, e) => NotifyFile(e.FullPath);
                logs.Renamed += (_, e) => { NotifyFile(e.OldFullPath); NotifyFile(e.FullPath); };
                logs.Error += (_, _) => Reconcile();
                var directories = new FileSystemWatcher(root)
                { IncludeSubdirectories = true, NotifyFilter = NotifyFilters.DirectoryName, InternalBufferSize = 16384 };
                watchers.Add(directories);
                void DirectoryChanged(string path) { if (_files.IsLogDirectory(path)) Reconcile(); }
                directories.Created += (_, e) => DirectoryChanged(e.FullPath);
                directories.Deleted += (_, e) => DirectoryChanged(e.FullPath);
                directories.Renamed += (_, e) => { DirectoryChanged(e.OldFullPath); DirectoryChanged(e.FullPath); };
                directories.Error += (_, _) => Reconcile();
                foreach (var watcher in watchers) watcher.EnableRaisingEvents = true;
            }
            _watchers = watchers.ToArray();
            _worker = Task.Run(RunAsync);
            Signal();
        }
        catch
        {
            foreach (var watcher in watchers) watcher.Dispose();
            _stop.Dispose(); _wake.Dispose(); throw;
        }
    }

    internal void NotifyFile(string path)
    {
        if (Volatile.Read(ref _disposed) != 0) return;
        var normalized = _files.NormalizeLogPath(path);
        if (normalized == null) return;
        Interlocked.Increment(ref _notifications);
        _dirty.TryAdd(normalized, 0); Signal();
    }
    internal void Reconcile()
    { if (Volatile.Read(ref _disposed) == 0) { Interlocked.Exchange(ref _rescan, 1); Signal(); } }
    private void Signal()
    {
        if (Volatile.Read(ref _disposed) != 0 || Interlocked.CompareExchange(ref _signalled, 1, 0) != 0) return;
        try { _wake.Release(); } catch (ObjectDisposedException) { }
    }

    private async Task RunAsync()
    {
        var retry = false;
        try
        {
            while (!_stop.IsCancellationRequested)
            {
                var notified = retry ? await _wake.WaitAsync(TimeSpan.FromSeconds(2), _stop.Token)
                    : await WaitAsync();
                if (notified)
                {
                    // Coalesce bursts. Idle logging has no periodic scan/poll.
                    await Task.Delay(200, _stop.Token);
                    Interlocked.Exchange(ref _signalled, 0);
                }
                var error = "";
                try
                {
                    if (Interlocked.Exchange(ref _rescan, 0) != 0)
                    {
                        _dirty.Clear();
                        var plan = _files.Inspect(_stop.Token);
                        _scans++; _index.Clear(); _oldest.Clear(); _retryAfter.Clear(); _bytes = 0;
                        foreach (var entry in plan.Entries) Put(entry);
                        if (plan.Unavailable > 0) error = "部分日志无法读取或属于链接。";
                    }
                    else
                        foreach (var path in _dirty.Keys.ToArray())
                            if (_dirty.TryRemove(path, out _)) Refresh(path);
                    retry = Trim();
                }
                catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
                { error = exception.Message; retry = false; }
                Volatile.Write(ref _status, new Status(_bytes, _index.Count, _scans, _reads,
                    Interlocked.Read(ref _notifications), _deleted, error));
            }
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested) { }
        finally { _index.Clear(); _oldest.Clear(); _dirty.Clear(); _wake.Dispose(); _stop.Dispose(); }
    }
    private async Task<bool> WaitAsync() { await _wake.WaitAsync(_stop.Token); return true; }

    private void Put(LogCleanup.Entry entry)
    { Remove(entry.Path); _index[entry.Path] = entry; _oldest.Add(entry); _bytes += entry.Identity.Bytes; }
    private void Remove(string path)
    {
        if (_index.Remove(path, out var old)) { _oldest.Remove(old); _bytes -= old.Identity.Bytes; }
        _retryAfter.Remove(path);
    }
    private void Refresh(string path)
    {
        _reads++;
        try
        {
            var entry = _files.ReadEntry(path);
            if (entry == null) Remove(path); else Put(entry);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            if (error is FileNotFoundException or DirectoryNotFoundException or LogCleanup.UnsafeLogPathException
                || error.InnerException is Win32Exception { NativeErrorCode: 2 or 3 }) Remove(path);
            // An inaccessible file is not zero bytes: retain its last known size.
        }
    }
    private bool Trim()
    {
        if (_bytes <= _limit) return false;
        var retry = false;
        foreach (var entry in _oldest.ToArray())
        {
            if (_bytes <= _limit || _stop.IsCancellationRequested) break;
            if (IsCurrentStream(entry.Path)) continue;
            if (_retryAfter.TryGetValue(entry.Path, out var next) && Environment.TickCount64 < next) { retry = true; continue; }
            try
            {
                if (_files.DeleteUnchanged(entry, _stop.Token)) { Remove(entry.Path); _deleted++; continue; }
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
            Refresh(entry.Path);
            if (_index.ContainsKey(entry.Path)) { _retryAfter[entry.Path] = Environment.TickCount64 + 2000; retry = true; }
        }
        return _bytes > _limit && retry;
    }
    private static bool IsCurrentStream(string path)
    {
        // Maa briefly closes maafw.log while copying it to its .bak block.
        // Never race that copy/truncate cycle or the Python focus readers.
        var name = Path.GetFileName(path);
        return name.Equals("maafw.log", StringComparison.OrdinalIgnoreCase)
            || name.Equals("adventure.log", StringComparison.OrdinalIgnoreCase)
            || name.Equals("run.log", StringComparison.OrdinalIgnoreCase)
            || name.Equals("score.log", StringComparison.OrdinalIgnoreCase)
            || name.Equals("history.log", StringComparison.OrdinalIgnoreCase);
    }
    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0) return;
        foreach (var watcher in _watchers) watcher.Dispose();
        try { _stop.Cancel(); } catch (ObjectDisposedException) { }
        // Finish only an already-submitted single-file operation.
    }
}
