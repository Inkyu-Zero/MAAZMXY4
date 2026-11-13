using System;
using System.Threading;

namespace MFAAvalonia.Services;

/// <summary>One advisory launch attempt for the entire assistant process, independent of tasks.</summary>
public sealed class BoxStartupAttempt
{
    private int _attempted;

    public void Try(Action launch, Func<bool> alreadyRunning, Action<string> log)
    {
        if (Interlocked.Exchange(ref _attempted, 1) != 0) return;
        try
        {
            if (alreadyRunning()) { log("盒子已运行，本次启动不再打开副本。"); return; }
            launch();
            log("已自动尝试启动盒子；需要使用助手时可点击“启动并连接盒子”。");
        }
        catch (Exception error) { log("自动启动盒子未完成：" + error.Message + "；可稍后手动启动，任务设置不变。"); }
    }
}
