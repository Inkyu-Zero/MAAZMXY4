using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using MFAAvalonia.Extensions.MaaFW;

namespace MFAAvalonia.Services;

/// <summary>Shared with the host task-start gate; never lock across an await.</summary>
public static class ScriptStudioGuard
{
    public static object SyncRoot { get; } = new();
    public static bool IsCapturing { get; set; }
    public static string UpdateBlockReason { get; set; } = "";
    public static int GameManagementOperations { get; set; }
    public static void ThrowIfCapturing()
    {
        if (!string.IsNullOrEmpty(UpdateBlockReason)) throw new InvalidOperationException(UpdateBlockReason);
        if (GameManagementOperations != 0) throw new InvalidOperationException("游戏管理正在重建连接，请稍候再开始任务或录制。");
        if (IsCapturing) throw new InvalidOperationException("请先在脚本工具中停止录制，再开始任务。");
    }
    public static void BeginCapture()
    {
        lock (SyncRoot)
        {
            ThrowIfCapturing();
            if (MaaProcessor.Processors.Any(p => p.IsTaskRunActive))
                throw new InvalidOperationException("有任务正在运行，请先停止任务再录制。");
            IsCapturing = true;
        }
    }
    public static void EndCapture() { lock (SyncRoot) IsCapturing = false; }
}

/// <summary>One owned Python worker. Stdout is a framed JSON-lines protocol.</summary>
public sealed class ScriptStudioClient : IDisposable
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Process? _process;
    private string _lastError = "";
    private long _sequence;
    private volatile bool _disconnected;
    private readonly bool _releaseCaptureOnDisconnect;
    public string ProjectRoot { get; }
    public bool IsDisconnected => _disconnected;

    public ScriptStudioClient(string? projectRoot = null, bool releaseCaptureOnDisconnect = true)
    { ProjectRoot = projectRoot ?? FindRoot(); _releaseCaptureOnDisconnect = releaseCaptureOnDisconnect; }

    private static string FindRoot()
    {
        var configured = Environment.GetEnvironmentVariable("MAAZMXY4_PROJECT_ROOT");
        if (!string.IsNullOrEmpty(configured) && File.Exists(Path.Combine(configured, "agent", "script_studio_service.py")))
            return Path.GetFullPath(configured);
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory != null; directory = directory.Parent)
            if (File.Exists(Path.Combine(directory.FullName, "agent", "script_studio_service.py"))) return directory.FullName;
        return AppContext.BaseDirectory;
    }

    private void EnsureStarted()
    {
        if (_disconnected) throw new IOException("脚本工具连接已中断。请重新打开前端，未保存内容会作为恢复草稿出现在脚本库。");
        if (_process is { HasExited: false }) return;
        if (_process != null) throw new IOException("脚本工具进程已退出，请重新打开前端并检查恢复草稿。");
        var python = Path.Combine(ProjectRoot, "runtimes", "python", "python.exe");
        var service = Path.Combine(ProjectRoot, "agent", "script_studio_service.py");
        if (!File.Exists(python) || !File.Exists(service))
            throw new InvalidOperationException("脚本工具运行文件不完整，请检查 agent 和 runtimes/python。");
        var info = new ProcessStartInfo(python)
        {
            WorkingDirectory = ProjectRoot, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardInputEncoding = new UTF8Encoding(false), StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        foreach (var argument in new[] { "-E", "-s", "-u", "-X", "utf8", service, "--root", ProjectRoot })
            info.ArgumentList.Add(argument);
        _process = new Process { StartInfo = info, EnableRaisingEvents = true };
        _process.ErrorDataReceived += (_, e) => { if (e.Data != null) _lastError = e.Data; };
        _process.Exited += (_, _) => { _disconnected = true; if (_releaseCaptureOnDisconnect) ScriptStudioGuard.EndCapture(); };
        _process.Start();
        _process.BeginErrorReadLine();
    }

    public async Task<JsonNode> CallAsync(string method, JsonObject? parameters = null, CancellationToken cancellationToken = default)
    {
        await _gate.WaitAsync(cancellationToken);
        try
        {
            EnsureStarted();
            var id = Interlocked.Increment(ref _sequence);
            var request = new JsonObject { ["id"] = id, ["method"] = method, ["params"] = parameters ?? new JsonObject() };
            await _process!.StandardInput.WriteLineAsync(request.ToJsonString());
            await _process.StandardInput.FlushAsync();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(timeout.Token, cancellationToken);
            string? line;
            try { line = await _process.StandardOutput.ReadLineAsync(linked.Token); }
            catch (OperationCanceledException)
            {
                _disconnected = true; Dispose();
                cancellationToken.ThrowIfCancellationRequested();
                throw new IOException("脚本工具响应超时，连接已关闭。请重新打开前端，在脚本库查找恢复草稿。");
            }
            if (line == null) throw new IOException("脚本工具连接已结束。" + _lastError);
            JsonObject response;
            long responseId;
            bool ok;
            try
            {
                response = JsonNode.Parse(line) as JsonObject ?? throw new IOException("脚本工具返回了无效响应。");
                responseId = response["id"]!.GetValue<long>();
                ok = response["ok"]!.GetValue<bool>();
            }
            catch (Exception error) when (error is JsonException or InvalidOperationException or FormatException or NullReferenceException)
            { throw new IOException("脚本工具响应损坏，连接已关闭，请重新打开前端检查恢复草稿。", error); }
            if (responseId != id)
            {
                _disconnected = true; Dispose();
                throw new IOException("脚本工具响应顺序错误，连接已关闭，请重新打开客户端。");
            }
            if (!ok)
                throw new InvalidOperationException(response["error"]?.GetValue<string>() ?? "操作未完成。");
            return response["result"]?.DeepClone() ?? new JsonObject();
        }
        catch (IOException)
        {
            _disconnected = true;
            Dispose();
            throw;
        }
        finally { _gate.Release(); }
    }

    public void Dispose()
    {
        _disconnected = true;
        var process = _process;
        _process = null;
        if (process == null) return;
        try
        {
            if (!process.HasExited)
            {
                process.StandardInput.WriteLine("{\"method\":\"shutdown\"}");
                process.StandardInput.Flush();
                process.StandardInput.Close();
                if (!process.WaitForExit(2500)) process.Kill(entireProcessTree: true);
            }
        }
        catch (InvalidOperationException) { }
        catch (IOException) { }
        finally { process.Dispose(); if (_releaseCaptureOnDisconnect) ScriptStudioGuard.EndCapture(); }
    }
}
