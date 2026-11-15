using System;
using System.Runtime.CompilerServices;
using System.Threading;

namespace MFAAvalonia.Services;

/// <summary>Release consumed native history only at a host-owned idle boundary.</summary>
public static class NativeTaskHistory
{
    private sealed class State { public int Users; }
    private static readonly ConditionalWeakTable<object, State> States = new();

    private sealed class Lease(State state) : IDisposable
    {
        private State? _state = state;
        public void Dispose()
        {
            var current = Interlocked.Exchange(ref _state, null);
            if (current != null) lock (current) current.Users--;
        }
    }

    public static IDisposable Acquire(object owner)
    {
        var state = States.GetValue(owner, _ => new State());
        lock (state) state.Users++;
        return new Lease(state);
    }

    public static T Use<T>(object owner, Func<T> operation)
    {
        using var lease = Acquire(owner);
        return operation();
    }

    // ClearCache also removes job ID mappings. Never call from a node callback,
    // during Wait(), or while a stop/queued native job can still use those IDs.
    // Windows callbacks copy focus/draw images synchronously before Wait returns.
    public static void ClearIfIdle(object owner, Func<bool> isIdle, Func<bool> clear, Action<string> log)
    {
        var state = States.GetValue(owner, _ => new State());
        // Serialize the idle decision and clear against new task/stop owners.
        // An operation holds a lease across its entire Wait, not just Post.
        lock (state)
        {
            if (state.Users != 0) return;
            try
            {
                if (isIdle() && !clear()) log("原生任务历史缓存未清理，将在下次空闲边界重试。");
            }
            catch (Exception error)
            {
                // Keep the consumed task outcome and its original exception.
                log("原生任务历史缓存清理跳过：" + error.Message);
            }
        }
    }
}
