using MFAAvalonia.Services;

var owner = new object();
var clears = 0;
var logs = new List<string>();
void Clear(bool idle = true) => NativeTaskHistory.ClearIfIdle(owner, () => idle, () => { clears++; return true; }, logs.Add);
void Check(bool value, string name) { if (!value) throw new Exception(name); Console.WriteLine("PASS " + name); }
Clear(false);
Check(clears == 0, "running/stopping jobs retain their history");
using (var task = NativeTaskHistory.Acquire(owner))
{
    Clear();
    using (var stop = NativeTaskHistory.Acquire(owner)) Clear();
    Clear();
    Check(clears == 0, "task and stop waiters protect IDs until consumed");
    NativeTaskHistory.ClearIfIdle(new object(), () => true, () => { clears++; return true; }, logs.Add);
    Check(clears == 1, "different taskers have independent ownership");
}
Clear();
Check(clears == 2, "idle results are released after the final waiter");
var lease = NativeTaskHistory.Acquire(owner); lease.Dispose(); lease.Dispose(); Clear();
Check(clears == 3, "duplicate disposal cannot corrupt the waiter count");
try { NativeTaskHistory.Use<int>(owner, () => throw new OperationCanceledException()); }
catch (OperationCanceledException) { }
Clear();
Check(clears == 4, "cancelled wait releases only its own lease");
NativeTaskHistory.ClearIfIdle(owner, () => true, () => false, logs.Add);
NativeTaskHistory.ClearIfIdle(owner, () => true, () => throw new ObjectDisposedException("old tasker"), logs.Add);
Check(logs.Count == 2, "cleanup failures remain diagnostics");
using var entered = new ManualResetEventSlim();
using var release = new ManualResetEventSlim();
var worker = Task.Run(() => NativeTaskHistory.Use(owner, () => { entered.Set(); release.Wait(); return 42; }));
Check(entered.Wait(2000), "stop waiter started");
Clear();
Check(clears == 4, "parallel cleanup cannot clear a live stop waiter");
release.Set();
Check(await worker == 42, "original stop result survives cleanup attempts");
Clear();
Check(clears == 5, "parallel waiter completion makes history reclaimable");
