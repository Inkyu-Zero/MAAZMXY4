using System;
using System.IO;
using System.Text.Json;
using System.Collections.Generic;

namespace MFAAvalonia.Services;

public static class GameManagementFeatures
{
    public static bool FirstUseTutorialEnabled => false;
}

/// <summary>Instance-owned settings. No account password or native handle is persisted.</summary>
public sealed record GameManagementOptions
{
    public bool RestartOnFailure { get; init; }
    public bool RestartOnMemory { get; init; }
    public bool RestartOnInterval { get; init; }
    public int IntervalMinutes { get; init; } = 60;
    public string ExePath { get; init; } = "";
    public string AccountName { get; init; } = "";
    public int SaveSlot { get; init; } = 1;
    public string RoleName { get; init; } = "";
    public bool Enabled => RestartOnFailure || RestartOnMemory || RestartOnInterval;
    public const long MemoryLimitBytes = 1_800_000_000; // 1.8 GB, decimal bytes.

    public void Validate(bool requireTarget = false, bool requireLogin = true)
    {
        if (IntervalMinutes is < 1 or > 10080)
            throw new InvalidDataException("重启间隔请输入 1～10080 分钟。");
        if (SaveSlot is < 1 or > 8)
            throw new InvalidDataException("请选择第 1～8 个已有存档。");
        if (!Enabled && !requireTarget) return;
        if (string.IsNullOrWhiteSpace(ExePath) || !Path.IsPathFullyQualified(ExePath)
            || !ExePath.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("请选择现有造梦盒子的完整 EXE 路径。");
        if (requireLogin && (string.IsNullOrWhiteSpace(AccountName) || AccountName.Length > 100
            || string.IsNullOrWhiteSpace(RoleName) || RoleName.Length > 100))
            throw new InvalidDataException("请填写盒子的历史账号名称及存档角色名，用于恢复时核对。");
    }

    public static GameManagementOptions Parse(string json)
    {
        var result = JsonSerializer.Deserialize<GameManagementOptions>(json)
            ?? throw new InvalidDataException("游戏管理设置为空。");
        result.Validate();
        return result;
    }
}

/// <summary>A cheap process sample only requests maintenance; the queue owns all input.</summary>
public sealed class GameRestartSchedule
{
    private int _highMemorySamples;
    private long _processStarted;
    private DateTimeOffset _nextRestart;
    private readonly Queue<DateTimeOffset> _memoryRestarts = new();
    public string? PendingReason { get; private set; }
    public bool MemoryRecoveryExhausted { get; private set; }

    public bool Observe(GameManagementOptions options, long privateBytes, long processStarted, DateTimeOffset now)
    {
        if (_processStarted != processStarted)
        {
            _processStarted = processStarted;
            _nextRestart = now.AddMinutes(options.IntervalMinutes);
            _highMemorySamples = 0;
            PendingReason = null;
        }
        if (PendingReason != null) return false;
        _highMemorySamples = options.RestartOnMemory && privateBytes > GameManagementOptions.MemoryLimitBytes
            ? _highMemorySamples + 1 : 0;
        while (_memoryRestarts.TryPeek(out var earlier) && now - earlier >= TimeSpan.FromMinutes(10)) _memoryRestarts.Dequeue();
        if (_highMemorySamples >= 2)
        {
            if (_memoryRestarts.Count >= 3) { MemoryRecoveryExhausted = true; return false; }
            PendingReason = "memory_limit";
        }
        else if (options.RestartOnInterval && now >= _nextRestart) PendingReason = "restart_interval";
        return PendingReason != null;
    }

    public void CompleteRestart(DateTimeOffset now)
    {
        if (PendingReason == "memory_limit") _memoryRestarts.Enqueue(now);
        _processStarted = 0; _highMemorySamples = 0; PendingReason = null;
    }
    public void Reset()
    {
        _processStarted = 0; _highMemorySamples = 0; PendingReason = null;
        _memoryRestarts.Clear(); MemoryRecoveryExhausted = false;
    }
}
