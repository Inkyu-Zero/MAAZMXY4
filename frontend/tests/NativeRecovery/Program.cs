using System.Text.Json.Nodes;
using MFAAvalonia.Services;

var tests = new List<(string Name, Func<Task> Run)>();
var enabled = new TaskRecoveryOptions(true, 3, 1, Path.GetFullPath("game.exe"));
TaskAttempt Good() => new(TaskAttemptStatus.Success);
TaskAttempt White() => new(TaskAttemptStatus.Recoverable, "white_screen");
TaskAttempt Ready() => new(TaskAttemptStatus.Ready);
void Require(bool value) { if (!value) throw new Exception("Assertion failed"); }
async Task Throws<T>(Func<Task> action) where T : Exception
{
    try { await action(); }
    catch (T) { return; }
    throw new Exception("Expected " + typeof(T).Name);
}
Task<TaskAttempt> NoRecovery(int slot, int attempt, CancellationToken token) => throw new Exception("Recovery must not run");
void Test(string name, Func<Task> run) => tests.Add((name, run));
void Sync(string name, Action action) => Test(name, () => { action(); return Task.CompletedTask; });

Sync("infinite brush count uses native unlimited sentinel without changing action parameters", () =>
{
    var result = JsonNode.Parse(TaskScriptPlan.NormalizeRepeatLimits("{\"loop\":{\"max_hit\":-1,\"custom_action_param\":{\"max_hit\":-1}},\"finite\":{\"max_hit\":3}}"))!;
    Require(result["loop"]!["max_hit"]!.GetValue<long>() == uint.MaxValue);
    Require(result["finite"]!["max_hit"]!.GetValue<int>() == 3);
    Require(result["loop"]!["custom_action_param"]!["max_hit"]!.GetValue<int>() == -1);
    var array = JsonNode.Parse(TaskScriptPlan.NormalizeRepeatLimits("[{\"loop\":{\"max_hit\":-1}}]"))!;
    Require(array[0]!["loop"]!["max_hit"]!.GetValue<long>() == uint.MaxValue);
});

Test("success completes exactly one round", async () =>
{
    var calls = 0;
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled, _ => { calls++; return Task.FromResult(Good()); }, NoRecovery, default);
    Require(result.Status == TaskAttemptStatus.Success && calls == 1);
});
Test("disabled recovery stops after first failure", async () =>
{
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled with { Enabled = false }, _ => Task.FromResult(White()), NoRecovery, default);
    Require(result.Status == TaskAttemptStatus.Recoverable);
});
Test("fatal and unknown failures never restart", async () =>
{
    foreach (var outcome in new[] { TaskAttempt.Fatal("unaccepted"), new TaskAttempt(TaskAttemptStatus.Recoverable, "unknown_screen") })
    {
        var result = await TaskRecoveryPolicy.RunRoundAsync(enabled, _ => Task.FromResult(outcome), NoRecovery, default);
        Require(result.Status == TaskAttemptStatus.Fatal);
    }
});
Test("recovery retries same round without extra completion", async () =>
{
    var runs = 0; var restarts = 0;
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => Task.FromResult(++runs == 1 ? White() : Good()),
        (slot, attempt, _) => { Require(slot == 1 && attempt == 1); restarts++; return Task.FromResult(Ready()); }, default);
    Require(result.Status == TaskAttemptStatus.Success && runs == 2 && restarts == 1);
});
Test("three restart budget is enforced", async () =>
{
    var runs = 0; var restarts = 0;
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => { runs++; return Task.FromResult(White()); },
        (_, attempt, _) => { Require(attempt == ++restarts); return Task.FromResult(Ready()); }, default);
    Require(result.Status == TaskAttemptStatus.Fatal && runs == 4 && restarts == 3);
});
Test("success resets the next MFA round budget", async () =>
{
    var restarts = 0;
    for (var round = 0; round < 2; round++)
    {
        var runs = 0;
        var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
            _ => Task.FromResult(++runs < 4 ? White() : Good()),
            (_, attempt, _) => { Require(attempt is >= 1 and <= 3); restarts++; return Task.FromResult(Ready()); }, default);
        Require(result.Status == TaskAttemptStatus.Success);
    }
    Require(restarts == 6);
});
Test("cancellation before task prevents all work", async () =>
{
    using var cancel = new CancellationTokenSource(); cancel.Cancel();
    await Throws<OperationCanceledException>(() => TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => throw new Exception("Task must not run"), NoRecovery, cancel.Token));
});
Test("stop during failed task prevents restart", async () =>
{
    using var cancel = new CancellationTokenSource();
    await Throws<OperationCanceledException>(() => TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => { cancel.Cancel(); return Task.FromResult(White()); }, NoRecovery, cancel.Token));
});
Test("stop during restart prevents retry input", async () =>
{
    using var cancel = new CancellationTokenSource(); var calls = 0;
    await Throws<OperationCanceledException>(() => TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => { calls++; return Task.FromResult(White()); },
        (_, _, _) => { cancel.Cancel(); return Task.FromResult(Ready()); }, cancel.Token));
    Require(calls == 1);
});
Test("startup failure ends recovery without another round", async () =>
{
    var calls = 0;
    await Throws<IOException>(() => TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => { calls++; return Task.FromResult(White()); },
        (_, _, _) => throw new IOException("start failed"), default));
    Require(calls == 1);
});
Test("login fatal stops without replaying round", async () =>
{
    var calls = 0;
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => { calls++; return Task.FromResult(White()); },
        (_, _, _) => Task.FromResult(TaskAttempt.Fatal("captcha")), default);
    Require(result.Status == TaskAttemptStatus.Fatal && calls == 1);
});
Test("white screen during login consumes remaining restart budget", async () =>
{
    var calls = 0; var retries = 0;
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => { calls++; return Task.FromResult(White()); },
        (_, _, _) => { retries++; return Task.FromResult(White()); }, default);
    Require(result.Status == TaskAttemptStatus.Fatal && retries == 3 && calls == 1);
});
Sync("stale and cross-instance checkpoints are rejected", () =>
{
    JsonObject State(string run, string instance, string status) => new() { ["version"] = 1, ["run_id"] = run, ["instance_id"] = instance, ["status"] = status };
    Require(TaskRecoveryPolicy.ReadCheckpoint(State("old", "a", "success"), "new", "a", false).Status == TaskAttemptStatus.Fatal);
    Require(TaskRecoveryPolicy.ReadCheckpoint(State("new", "b", "success"), "new", "a", false).Status == TaskAttemptStatus.Fatal);
    Require(TaskRecoveryPolicy.ReadCheckpoint(State("new", "a", "running"), "new", "a", false).Status == TaskAttemptStatus.Fatal);
    Require(TaskRecoveryPolicy.ReadCheckpoint(State("new", "a", "ready"), "new", "a", false).Status == TaskAttemptStatus.Fatal);
    Require(TaskRecoveryPolicy.ReadCheckpoint(State("new", "a", "success"), "new", "a", true).Status == TaskAttemptStatus.Fatal);
    Require(TaskRecoveryPolicy.ReadCheckpoint(State("new", "a", "ready"), "new", "a", true).Status == TaskAttemptStatus.Ready);
});
Sync("only the selected PID and start identity can be ended", () =>
{
    var expected = new TaskProcessIdentity(12, 100, Path.GetFullPath("game.exe"));
    expected.RequireSame(expected);
    foreach (var wrong in new[] { expected with { Pid = 13 }, expected with { StartedUtcTicks = 101 }, expected with { ExePath = Path.GetFullPath("other.exe") } })
    {
        try { expected.RequireSame(wrong); throw new Exception("Identity should fail"); }
        catch (InvalidOperationException) { }
    }
});
Sync("configured executable cannot silently target a launcher", () =>
{
    var expected = new TaskProcessIdentity(12, 100, Path.GetFullPath("game.exe"));
    try { expected.RequireConfiguredPath(Path.GetFullPath("launcher.exe")); throw new Exception("Path should fail"); }
    catch (InvalidOperationException) { }
});
Sync("other running instance sharing the PID blocks restart", () =>
{
    TaskRecoveryPolicy.RequireExclusivePid(12, new[] { 13, 14 });
    try { TaskRecoveryPolicy.RequireExclusivePid(12, new[] { 12, 14 }); throw new Exception("Shared process should fail"); }
    catch (InvalidOperationException) { }
});
Sync("old process windows are rejected and new ambiguity stops", () =>
{
    Require(!TaskRecoveryPolicy.SelectUniqueWindow(new[] { (Pid: 1, Handle: 11) }, w => w.Pid, 2).Found);
    var selected = TaskRecoveryPolicy.SelectUniqueWindow(new[] { (Pid: 1, Handle: 11), (Pid: 2, Handle: 21) }, w => w.Pid, 2);
    Require(selected.Found && selected.Window.Handle == 21);
    try { TaskRecoveryPolicy.SelectUniqueWindow(new[] { (Pid: 2, Handle: 21), (Pid: 2, Handle: 22) }, w => w.Pid, 2); throw new Exception("Ambiguity should fail"); }
    catch (InvalidOperationException) { }
});
Sync("policy rejects unreviewable paths and invalid limits", () =>
{
    foreach (var invalid in new[]
    {
        "{\"enabled\":true,\"exe_path\":\"game.exe\"}",
        "{\"enabled\":true,\"exe_path\":\"C:/game.cmd\"}",
        "{\"max_retries\":4}", "{\"save_slot\":0}"
    })
    {
        try { TaskRecoveryOptions.Read(new() { ["recovery"] = JsonNode.Parse(invalid) }); throw new Exception("Policy should fail"); }
        catch (InvalidDataException) { }
    }
    Require(!TaskRecoveryOptions.Read(new()).Enabled);
});
Sync("complete native parameters survive object replacement semantics", () =>
{
    var parameters = new JsonObject { ["file"] = "chosen.json", ["exit_mode"] = "battle_only" };
    TaskRecoveryPolicy.CompleteParameters(parameters, enabled, false);
    parameters["run_id"] = "nonce";
    Require(parameters["phase"]!.GetValue<string>() == "begin");
    Require(parameters["file"]!.GetValue<string>() == "chosen.json" && parameters["exit_mode"]!.GetValue<string>() == "battle_only");
    Require(parameters["recovery"]!["enabled"]!.GetValue<bool>());
    var defaults = new JsonObject();
    TaskRecoveryPolicy.CompleteParameters(defaults, enabled with { SaveSlot = 4 }, true);
    Require(defaults["phase"]!.GetValue<string>() == "recovery_begin");
    Require(defaults["file"] == null);
    Require(defaults["exit_mode"] == null);
    Require(defaults["save_slot"]!.GetValue<int>() == 4);
});
Sync("actual MaaToken ordered option array keeps file mode and enabled recovery inputs", () =>
{
    const string entry = "悬赏（任务）-初始化";
    JsonObject Override(JsonObject parameters) => new()
    {
        [entry] = new JsonObject { ["action"] = new JsonObject { ["param"] = new JsonObject { ["custom_action_param"] = parameters } } }
    };
    var serialized = new JsonArray
    {
        Override(new() { ["file"] = "@selected:strict" }),
        Override(new() { ["exit_mode"] = "battle_only" }),
        Override(new() { ["recovery"] = new JsonObject { ["enabled"] = true } }),
        Override(new() { ["recovery"] = new JsonObject { ["exe_path"] = enabled.ExePath } }),
        Override(new() { ["recovery"] = new JsonObject { ["save_slot"] = "4", ["max_retries"] = "2" } })
    }.ToJsonString();
    var merged = TaskRecoveryPolicy.MergePipelineOverrides(serialized);
    var parameters = merged[entry]!["action"]!["param"]!["custom_action_param"]!.AsObject();
    var options = TaskRecoveryOptions.Read(parameters);
    TaskRecoveryPolicy.CompleteParameters(parameters, options, false);
    Require(parameters["file"]!.GetValue<string>() == "@selected:strict");
    Require(parameters["exit_mode"]!.GetValue<string>() == "battle_only");
    Require(parameters["phase"]!.GetValue<string>() == "begin");
    Require(options.Enabled && options.SaveSlot == 4 && options.MaxRetries == 2 && options.ExePath == enabled.ExePath);
    Require(merged[entry]!["action"]!["param"]!["custom_action_param"]!["recovery"]!["enabled"]!.GetValue<bool>());
});
Sync("option merge respects order and replaces next lists", () =>
{
    var result = TaskRecoveryPolicy.MergePipelineOverrides("[{\"node\":{\"next\":[\"old\"],\"flag\":1}},{\"node\":{\"next\":[\"new\"],\"flag\":2}}]");
    Require(result["node"]!["flag"]!.GetValue<int>() == 2);
    Require(result["node"]!["next"]!.AsArray().Count == 1 && result["node"]!["next"]![0]!.GetValue<string>() == "new");
    Require(TaskRecoveryPolicy.MergePipelineOverrides("[]").Count == 0);
    Require(TaskRecoveryPolicy.MergePipelineOverrides("{}").Count == 0);
    try { TaskRecoveryPolicy.MergePipelineOverrides("[17]"); throw new Exception("Invalid option should fail"); }
    catch (InvalidDataException) { }
});
Sync("same round pins script filename and bytes across automatic recovery", () =>
{
    var first = new JsonObject { ["recording_file"] = "任务.json", ["recording_sha256"] = new string('a', 64) };
    var revision = new TaskRecordingRevision();
    revision.Observe(first);
    var retry = new JsonObject(); revision.Apply(retry);
    Require(retry["expected_recording_file"]!.GetValue<string>() == "任务.json");
    Require(retry["expected_recording_sha256"]!.GetValue<string>() == new string('a', 64));
    revision.Observe(new()); // Login ready has no new recording and cannot clear the pin.
    foreach (var changed in new[]
    {
        new JsonObject { ["recording_file"] = "other.json", ["recording_sha256"] = new string('a', 64) },
        new JsonObject { ["recording_file"] = "任务.json", ["recording_sha256"] = new string('b', 64) }
    })
    {
        try { revision.Observe(changed); throw new Exception("Changed script should stop recovery"); }
        catch (InvalidDataException) { }
    }
    Require(revision.Sha256 == new string('a', 64));
});
Sync("next MFA round can adopt a newly saved script revision", () =>
{
    var next = new TaskRecordingRevision();
    next.Observe(new() { ["recording_file"] = "new.json", ["recording_sha256"] = new string('b', 64) });
    Require(next.FileName == "new.json" && next.Sha256 == new string('b', 64));
});
Sync("only matching capture failures may be checked for positive window loss", () =>
{
    JsonObject State(string status, string reason, string run = "new") => new()
    {
        ["version"] = 1, ["run_id"] = run, ["instance_id"] = "default", ["status"] = status, ["reason"] = reason
    };
    Require(TaskRecoveryPolicy.IsCaptureLossCandidate(State("running", ""), "new", "default"));
    Require(TaskRecoveryPolicy.IsCaptureLossCandidate(State("fatal", "capture_unavailable"), "new", "default"));
    foreach (var state in new[] { State("fatal", "unknown_screen"), State("fatal", "recording_changed"),
        State("fatal", "not_accepted"), State("cancelled", "capture_unavailable"), State("running", "", "old") })
        Require(!TaskRecoveryPolicy.IsCaptureLossCandidate(state, "new", "default"));
});
Sync("client restoration accounts for borders without changing recording scale", () =>
{
    var original = new TaskWindowSize(1175, 737);
    var target = original.OuterSizeFor(new(1000, 600), new(1016, 639));
    Require(target == new TaskWindowSize(1191, 776));
    Require(original.OuterSizeFor(original, new(1191, 776)) == new TaskWindowSize(1191, 776));
    foreach (var invalid in new[] { new TaskWindowSize(0, 720), new TaskWindowSize(720, -1), new TaskWindowSize(40000, 720) })
    {
        try { invalid.OuterSizeFor(new(1000, 600), new(1016, 639)); throw new Exception("Invalid size should stop recovery"); }
        catch (InvalidOperationException) { }
    }
    try { original.OuterSizeFor(new(1000, 600), new(900, 639)); throw new Exception("Invalid border should stop recovery"); }
    catch (InvalidOperationException) { }
});

Test("old native input and Agent finish before process termination", async () =>
{
    var lifecycle = new FakeLifecycle();
    var result = await TaskRecoveryPolicy.RestartAsync(lifecycle, 3, default);
    Require(result.Status == TaskAttemptStatus.Ready && lifecycle.Slot == 3);
    Require(string.Join(",", lifecycle.Calls) == "release,kill,start,bind,resize,connect,login");
});
Test("failed native input cleanup never kills the game", async () =>
{
    var lifecycle = new FakeLifecycle { FailAt = "release" };
    await Throws<IOException>(() => TaskRecoveryPolicy.RestartAsync(lifecycle, 1, default));
    Require(string.Join(",", lifecycle.Calls) == "release");
});
Test("cancellation at every lifecycle phase prevents subsequent work", async () =>
{
    var phases = new[] { "release", "kill", "start", "bind", "resize", "connect", "login" };
    for (var i = 0; i < phases.Length; i++)
    {
        using var cancel = new CancellationTokenSource();
        var lifecycle = new FakeLifecycle { CancelAfter = phases[i], Cancellation = cancel };
        await Throws<OperationCanceledException>(() => TaskRecoveryPolicy.RestartAsync(lifecycle, 1, cancel.Token));
        Require(lifecycle.Calls.SequenceEqual(phases.Take(i + 1)));
    }
});
Test("window ambiguity prevents reconnect and login input", async () =>
{
    var lifecycle = new FakeLifecycle { FailAt = "bind" };
    await Throws<IOException>(() => TaskRecoveryPolicy.RestartAsync(lifecycle, 1, default));
    Require(string.Join(",", lifecycle.Calls) == "release,kill,start,bind");
});
Test("reconnect failure never attempts login", async () =>
{
    var lifecycle = new FakeLifecycle { FailAt = "connect" };
    await Throws<IOException>(() => TaskRecoveryPolicy.RestartAsync(lifecycle, 1, default));
    Require(string.Join(",", lifecycle.Calls) == "release,kill,start,bind,resize,connect");
});

Sync("countdown displays remaining seconds instead of elapsed recording time", () =>
{
    foreach (var state in new[] { "countdown", "resume_countdown" })
    {
        var text = StudioCapturePresentation.Title(new JsonObject { ["state"] = state, ["countdown_seconds"] = 2.1, ["elapsed_ms"] = 0 });
        Require(text.Contains("3 秒") && !text.Contains("0.0"));
    }
    Require(StudioCapturePresentation.Title(JsonNode.Parse("{\"state\":\"paused\",\"elapsed_ms\":1500}")!.AsObject()).Contains("1.5"));
});
JsonObject Recording() => new() { ["file"] = "test.json", ["path"] = "C:/project/recordings/test.json", ["sha256"] = new string('a', 64) };
Test("unbound task logs once and uses the host stopped path", async () =>
{
    var logs = new List<string>();
    var plan = new JsonObject { ["handled"] = true, ["stop_reason"] = "unbound_script", ["message"] = "请先绑定脚本，本次任务已结束。" };
    await Throws<OperationCanceledException>(() => { TaskScriptPlan.StopIfUnbound(plan, logs.Add, default); return Task.CompletedTask; });
    Require(logs.Count == 1 && logs[0].Contains("绑定脚本"));
    TaskScriptPlan.StopIfUnbound(new JsonObject { ["handled"] = false }, logs.Add, default);
    Require(logs.Count == 1);
});

Sync("ordinary tasks retain flow and receive the selected script on playback nodes", () =>
{
    var plan = new JsonObject { ["binding"] = new JsonObject { ["mode"] = "pipeline" }, ["recording"] = Recording(), ["playback_nodes"] = new JsonArray("Fight", "FightAgain") };
    var result = TaskScriptPlan.Prepare(plan, "Start", "{\"Start\":{\"next\":[\"Fight\"]},\"Fight\":{\"action\":{\"param\":{\"custom_action_param\":{\"repeat\":2}}}}}");
    Require(result.Entry == "Start" && result.Profile == null);
    foreach (var node in new[] { "Fight", "FightAgain" })
        Require(TaskScriptPlan.Parameters(result.Pipeline, node)["file"]!.GetValue<string>().EndsWith("test.json"));
    Require(TaskScriptPlan.Parameters(result.Pipeline, "Fight")["repeat"]!.GetValue<int>() == 2);
    Require(result.Pipeline["Start"]!["next"]![0]!.GetValue<string>() == "Fight");
});
Sync("replacement binding executes only a standalone native playback node", () =>
{
    var plan = new JsonObject { ["binding"] = new JsonObject { ["mode"] = "replace" }, ["recording"] = Recording() };
    var result = TaskScriptPlan.Prepare(plan, "UnrelatedTask", "{\"UnrelatedTask\":{\"next\":[\"Other\"]}}");
    Require(result.Entry != "UnrelatedTask" && result.Pipeline.Count == 1 && result.Profile == null);
    Require(result.Pipeline[result.Entry]!["action"]!["param"]!["custom_action"]!.GetValue<string>() == "battle_play");
    Require(result.Pipeline[result.Entry]!["next"]!.AsArray().Count == 0);
});
Sync("configured task profiles merge shared options without hardcoded task names", () =>
{
    var plan = JsonNode.Parse("{\"id\":\"task-custom\",\"profile\":{\"resume_entry\":\"LoginAgain\",\"parameters\":{\"playback_node\":\"Play\",\"execution\":{\"settings_node\":\"Settings\"},\"routes\":{\"done\":[\"Exit\"]},\"exit_mode\":\"done\"}}}")!.AsObject();
    var result = TaskScriptPlan.Prepare(plan, "MyEntry", "[{\"Settings\":{\"action\":{\"param\":{\"custom_action_param\":{\"file\":\"@selected\",\"recovery\":{\"enabled\":false}}}}}}]");
    var values = TaskScriptPlan.Parameters(result.Pipeline, "MyEntry");
    Require(values["file"]!.GetValue<string>() == "@selected" && values["task_binding_id"]!.GetValue<string>() == "task-custom");
    Require(values["playback_node"]!.GetValue<string>() == "Play" && values["routes"]!["done"]![0]!.GetValue<string>() == "Exit");
    Require(result.Profile!["resume_entry"]!.GetValue<string>() == "LoginAgain");
});

Sync("recorded return skips native exit and verifies the configured map scene", () =>
{
    var plan = new JsonObject { ["binding"] = new JsonObject { ["mode"] = "pipeline" },
        ["recording"] = Recording(), ["playback_nodes"] = new JsonArray("Fight"),
        ["playback_routes"] = new JsonObject { ["Fight"] = new JsonObject { ["recorded_return_next"] = new JsonArray("ConfirmMap") } } };
    var overrides = new JsonObject { ["Fight"] = new JsonObject { ["next"] = new JsonArray("Exit") } };
    TaskScriptPlan.Parameters(overrides, "脚本任务设置")["exit_mode"] = "recorded_return";
    var result = TaskScriptPlan.Prepare(plan, "Enter", overrides.ToJsonString());
    Require(result.Entry == "Enter" && result.Pipeline["Fight"]!["next"]![0]!.GetValue<string>() == "ConfirmMap");
    TaskScriptPlan.Parameters(overrides, "脚本任务设置")["exit_mode"] = "battle_only";
    result = TaskScriptPlan.Prepare(plan, "Enter", overrides.ToJsonString());
    Require(result.Pipeline["Fight"]!["next"]![0]!.GetValue<string>() == "Exit");
});
Test("unsupported recorded-return routes fail before starting a native task", async () =>
{
    var plan = new JsonObject { ["binding"] = new JsonObject { ["mode"] = "pipeline" },
        ["recording"] = Recording(), ["playback_nodes"] = new JsonArray("Fight") };
    var overrides = new JsonObject();
    TaskScriptPlan.Parameters(overrides, "脚本任务设置")["exit_mode"] = "recorded_return";
    await Throws<InvalidDataException>(() => { TaskScriptPlan.Prepare(plan, "Enter", overrides.ToJsonString()); return Task.CompletedTask; });
});

Sync("Win32 selection never guesses from enumeration order or shared window classes", () =>
{
    var windows = new (nint Handle, string Title, string ClassName)[]
        { (1,"Unrelated app","Chrome"), (2,"Game","Chrome"), (3,"Game","Chrome") };
    Require(Win32WindowSelection.SelectIndex(windows, null, null) == -1);
    Require(Win32WindowSelection.SelectIndex(windows, null, ("Game","Chrome")) == -1);
    Require(Win32WindowSelection.SelectIndex(windows, (3,"Game","Chrome"), null) == 2);
    Require(Win32WindowSelection.SelectIndex(windows, (1,"Closed game","Chrome"), null) == -1);
    Require(Win32WindowSelection.SelectIndex(windows, null, ("Unknown","Chrome")) == -1);
    Require(Win32WindowSelection.SelectIndex(new[] { windows[1] }, null, ("Game","Chrome")) == 0);
    Require(Win32WindowSelection.SelectIndex(new[] { windows[1] }, null, null) == -1);
});

Sync("managed memory threshold needs two samples and produces one pending request", () =>
{
    var options = new GameManagementOptions { RestartOnMemory = true };
    var schedule = new GameRestartSchedule(); var now = DateTimeOffset.UnixEpoch;
    Require(!schedule.Observe(options, 1_800_000_000, 1, now));
    Require(!schedule.Observe(options, 1_800_000_001, 1, now.AddSeconds(5)));
    Require(schedule.Observe(options, 1_800_000_001, 1, now.AddSeconds(10)));
    Require(schedule.PendingReason == "memory_limit");
    Require(!schedule.Observe(options, 2_000_000_000, 1, now.AddSeconds(15)));
});
Sync("interval is elapsed minutes and a replacement process starts a new interval", () =>
{
    var options = new GameManagementOptions { RestartOnInterval = true, IntervalMinutes = 60 };
    var schedule = new GameRestartSchedule(); var now = DateTimeOffset.UnixEpoch;
    Require(!schedule.Observe(options, 0, 1, now));
    Require(!schedule.Observe(options, 0, 1, now.AddMinutes(59)));
    Require(schedule.Observe(options, 0, 1, now.AddMinutes(60)));
    schedule.CompleteRestart(now.AddMinutes(60));
    Require(!schedule.Observe(options, 0, 2, now.AddMinutes(61)));
    Require(!schedule.Observe(options, 0, 2, now.AddMinutes(120)));
    Require(schedule.Observe(options, 0, 2, now.AddMinutes(121)));
});
Sync("persistent memory pressure cannot cause an endless restart loop", () =>
{
    var options = new GameManagementOptions { RestartOnMemory = true };
    var schedule = new GameRestartSchedule(); var now = DateTimeOffset.UnixEpoch;
    for (var attempt = 1; attempt <= 3; attempt++)
    {
        Require(!schedule.Observe(options, 2_000_000_000, attempt, now));
        Require(schedule.Observe(options, 2_000_000_000, attempt, now.AddSeconds(5)));
        schedule.CompleteRestart(now.AddSeconds(10)); now = now.AddSeconds(30);
    }
    Require(!schedule.Observe(options, 2_000_000_000, 4, now));
    Require(!schedule.Observe(options, 2_000_000_000, 4, now.AddSeconds(5)));
    Require(schedule.MemoryRecoveryExhausted && schedule.PendingReason == null);
});
Test("managed defeat restarts the same round and completes only after confirmation", async () =>
{
    var calls = 0; var restarts = 0;
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => Task.FromResult(++calls == 1 ? new TaskAttempt(TaskAttemptStatus.Recoverable, "battle_failed") : Good()),
        (_, _, _) => { restarts++; return Task.FromResult(Ready()); }, default);
    Require(result.Status == TaskAttemptStatus.Success && calls == 2 && restarts == 1);
});
Sync("management stays disabled by default and requires a target for automation", () =>
{
    Require(!GameManagementOptions.Parse("{}").Enabled && !GameManagementFeatures.FirstUseTutorialEnabled);
    foreach (var json in new[] { "{\"IntervalMinutes\":0}", "{\"IntervalMinutes\":10081}", "{\"RestartOnFailure\":true}" })
    {
        try { GameManagementOptions.Parse(json); throw new Exception("Invalid settings accepted"); }
        catch (InvalidDataException) { }
    }
});

Test("lost-window interruption drains the old execution before recovery", async () =>
{
    var execution = new TaskCompletionSource<int>(); var stopped = 0;
    var observed = await TaskRecoveryPolicy.ObserveExecutionAsync(execution.Task, () => true,
        () => { stopped++; execution.SetResult(17); }, default, TimeSpan.FromMilliseconds(1));
    Require(observed.TargetLost && observed.Result == 17 && stopped == 1);
});
Test("user cancellation never requests a game restart", async () =>
{
    using var cancel = new CancellationTokenSource(); cancel.Cancel();
    await Throws<OperationCanceledException>(() => TaskRecoveryPolicy.ObserveExecutionAsync(
        new TaskCompletionSource<int>().Task, () => true, () => throw new Exception("Unexpected stop/restart"), cancel.Token));
});
Sync("managed native quotas stop at one confirmed round and preserve unrelated overrides", () =>
{
    var plan = new JsonObject { ["binding"] = new JsonObject { ["mode"] = "pipeline" },
        ["recording"] = Recording(), ["playback_nodes"] = new JsonArray("Fight"),
        ["management_profile"] = new JsonObject { ["entry"] = "Begin", ["success_node"] = "Done",
            ["resume_entry"] = "GameManagement_Resume", ["native_repeat"] = true, ["repeat_limit"] = 37,
            ["parameters"] = new JsonObject { ["phase"] = "begin", ["file"] = "@task", ["navigation"] = new JsonObject { ["kind"] = "stage" } } } };
    var result = TaskScriptPlan.PrepareManaged(plan, "Start", "{\"Done\":{\"post_delay\":42},\"Counter\":{\"max_hit\":37}}",
        new GameManagementOptions { RestartOnFailure=true,AccountName="account",RoleName="role" });
    Require(result.Entry == "Begin" && result.Limit == 37 && result.Pipeline["Done"]!["next"]!.AsArray().Count == 0);
    Require(result.Pipeline["Done"]!["post_delay"]!.GetValue<int>() == 42);
    Require(TaskScriptPlan.Parameters(result.Pipeline,"Begin")["expected_recording_sha256"]!.ToString() == Recording()["sha256"]!.ToString());
    Require(TaskScriptPlan.Parameters(result.Pipeline,"Begin")["game_management"]!["restart_on_failure"]!.GetValue<bool>());
    plan["management_profile"]!["repeat_limit"] = -1;
    result = TaskScriptPlan.PrepareManaged(plan,"Start","{\"Counter\":{\"max_hit\":-1}}",new());
    Require(result.Limit == long.MaxValue && result.Pipeline["Counter"]!["max_hit"]!.GetValue<long>() == uint.MaxValue);
    plan["management_profile"]!["native_repeat"] = false;
    Require(TaskScriptPlan.PrepareManaged(plan,"Start",null,new()).Limit == 1); // MFA owns its outer repetitions.
});
Sync("AFK overrides finite native quota and retry limit without changing saved input", () =>
{
    var raw = "{\"Counter\":{\"max_hit\":7},\"脚本任务设置\":{\"action\":{\"param\":{\"custom_action_param\":{\"afk_mode\":true,\"failure_retry\":{\"enabled\":false,\"max_retries\":0}}}}}}";
    var result = JsonNode.Parse(TaskScriptPlan.NormalizeRepeatLimits(raw, "Counter"))!;
    Require(result["Counter"]!["max_hit"]!.GetValue<long>() == uint.MaxValue);
    Require(TaskScriptPlan.Parameters(result.AsObject(), "脚本任务设置")["failure_retry"]!["max_retries"]!.GetValue<int>() == -1);
    Require(JsonNode.Parse(raw)!["Counter"]!["max_hit"]!.GetValue<int>() == 7);
});
Sync("legacy unlimited count implies in-game retries even with restart enabled", () =>
{
    var raw = """{"Counter":{"max_hit":-1},"脚本任务设置":{"action":{"param":{"custom_action_param":{"failure_retry":{"enabled":false,"max_retries":0}}}}}}""";
    var normalized = TaskScriptPlan.NormalizeRepeatLimits(raw, "Counter");
    Console.WriteLine("TRACE legacy unlimited normalized=" + normalized);
    Require(TaskScriptPlan.IsAfkMode(normalized));
    var plan = new JsonObject { ["binding"] = new JsonObject { ["mode"] = "pipeline" },
        ["recording"] = Recording(), ["playback_nodes"] = new JsonArray("Fight"),
        ["management_profile"] = new JsonObject { ["entry"] = "Begin", ["success_node"] = "Done",
            ["native_repeat"] = true, ["repeat_limit"] = -1,
            ["parameters"] = new JsonObject { ["failure_retry"] = new JsonObject { ["enabled"] = false, ["max_retries"] = 0 } } } };
    var result = TaskScriptPlan.PrepareManaged(plan, "Counter", raw, new() { RestartOnFailure = true });
    var settings = TaskScriptPlan.Parameters(result.Pipeline, "Begin");
    Require(settings["afk_mode"]?.GetValue<bool>() == true);
    Require(settings["failure_retry"]?["enabled"]?.GetValue<bool>() == true);
    Require(settings["failure_retry"]?["max_retries"]?.GetValue<int>() == -1);
    Require(result.Limit == long.MaxValue);
    plan["management_profile"]!["repeat_limit"]=7;
    Require(TaskScriptPlan.PrepareManaged(plan,"Counter",normalized,new()).Limit==long.MaxValue);
    plan["management_profile"]!["native_repeat"]=false;
    Require(TaskScriptPlan.PrepareManaged(plan,"Counter",normalized,new()).Limit==1); // Host owns the unlimited loop.
    var finite = """{"脚本任务设置":{"action":{"param":{"custom_action_param":{"afk_mode":false,"failure_retry":{"enabled":true,"max_retries":3}}}}}}""";
    var finitePlan=TaskScriptPlan.PrepareManaged(plan,"Begin",finite,new() { RestartOnFailure=true });
    Require(TaskScriptPlan.Parameters(finitePlan.Pipeline,"Begin")["failure_retry"]?["max_retries"]?.GetValue<int>()==3);
    Require(!TaskScriptPlan.IsAfkMode(finitePlan.Pipeline.ToJsonString()));
});
Test("process recovery remains bounded separately from unlimited in-game defeats", async () =>
{
    foreach (var reason in new[] { "battle_failed", "battle_retry_exhausted" })
    {
        var count = 0;
        var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
            _ => Task.FromResult(new TaskAttempt(TaskAttemptStatus.Recoverable, reason)),
            (_, _, _) => { count++; return Task.FromResult(Ready()); }, default);
        Require(count == 3 && result.Status == TaskAttemptStatus.Fatal);
    }
});
Test("AFK still bounds window-loss recovery", async () =>
{
    var count = 0;
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => Task.FromResult(new TaskAttempt(TaskAttemptStatus.Recoverable, "window_lost")),
        (_, _, _) => { count++; return Task.FromResult(Ready()); }, default);
    Require(count == 3 && result.Status == TaskAttemptStatus.Fatal);
});
Test("AFK alone never enables process restart", async () =>
{
    Require(!new GameManagementOptions().Enabled);
    foreach (var reason in new[] { "battle_failed", "battle_retry_exhausted", "battle_return_failed", "task_failed", "window_lost" })
    {
        var result = await TaskRecoveryPolicy.RunRoundAsync(enabled with { Enabled = false },
            _ => Task.FromResult(new TaskAttempt(TaskAttemptStatus.Recoverable, reason)), NoRecovery, default);
        Require(result.Status == TaskAttemptStatus.Recoverable);
    }
});
Sync("AIR and DPI scaling preserve normalized geometry but aspect changes do not", () =>
{
    Require(new TaskWindowSize(1148,720).SameNormalizedFrame(new TaskWindowSize(1176,738)));
    Require(new TaskWindowSize(940,590).SameNormalizedFrame(new TaskWindowSize(1880,1180)));
    Require(!new TaskWindowSize(1148,720).SameNormalizedFrame(new TaskWindowSize(1024,768)));
    new GameManagementOptions { SaveSlot = 8 }.Validate();
});

var gameIdentity = new TaskWindowIdentity("造梦盒子v1.9 - By Duskeye", "ApolloRuntimeContentWindow");
TaskWindowCandidate GameWindow(nint handle = 0xE1652, int pid = 20764) =>
    new(handle, pid, gameIdentity, new(1175, 737), new(1191, 776), true);
Sync("restart ignores the logged AIR placeholder and waits for the real game HWND", () =>
{
    var ready = new TaskWindowReadiness(20764, gameIdentity);
    var splash = GameWindow(0x4209B8) with { Identity = new("Adobe AIR", gameIdentity.ClassName), ClientSize = new(900, 525) };
    Require(ready.Observe(new[] { splash }, TimeSpan.Zero) == null);
    Require(ready.Observe(new[] { splash }, TimeSpan.FromSeconds(20)) == null);
    var real = GameWindow();
    Require(ready.Observe(new[] { splash, real }, TimeSpan.FromSeconds(21)) == null);
    Require(ready.Observe(new[] { real }, TimeSpan.FromSeconds(21.9)) == null);
    Require(ready.Observe(new[] { real }, TimeSpan.FromSeconds(22)) == real);
});
Sync("matching title on another PID or class never substitutes the verified game", () =>
{
    var ready = new TaskWindowReadiness(20764, gameIdentity);
    var candidates = new[] { GameWindow(pid: 42), GameWindow() with { Identity = gameIdentity with { ClassName = "OtherClass" } },
        GameWindow() with { Visible = false }, GameWindow(0) };
    Require(ready.Observe(candidates, TimeSpan.Zero) == null);
    Require(ready.Observe(candidates, TimeSpan.FromSeconds(59)) == null);
});
Sync("two real game windows are ambiguous even when a splash also exists", () =>
{
    var ready = new TaskWindowReadiness(20764, gameIdentity);
    try { ready.Observe(new[] { GameWindow(11), GameWindow(12) }, TimeSpan.Zero); throw new Exception("Ambiguity accepted"); }
    catch (InvalidOperationException) { }
});
Sync("startup bounds and HWND replacement reset readiness instead of resizing a placeholder", () =>
{
    var ready = new TaskWindowReadiness(20764, gameIdentity);
    var real = GameWindow();
    foreach (var bad in new[] { real with { ClientSize = new(0, 0) },
        real with { OuterSize = new(900, 525) }, real with { GeometryConsistent = false } })
    {
        Require(ready.Observe(new[] { real }, TimeSpan.Zero) == null);
        Require(ready.Observe(new[] { bad }, TimeSpan.FromSeconds(1)) == null);
        Require(ready.Observe(new[] { real }, TimeSpan.FromSeconds(2)) == null);
    }
    Require(ready.Observe(Array.Empty<TaskWindowCandidate>(), TimeSpan.FromSeconds(3)) == null);
    Require(ready.Observe(new[] { real }, TimeSpan.FromSeconds(4)) == null);
    var replacement = GameWindow(0xE1653);
    Require(ready.Observe(new[] { replacement }, TimeSpan.FromSeconds(5)) == null);
    Require(ready.Observe(new[] { replacement }, TimeSpan.FromSeconds(6)) == replacement);
});
Sync("fresh launch without a saved identity accepts only a stable box main window", () =>
{
    var ready = new TaskWindowReadiness(20764, null);
    Require(ready.Observe(new[] { GameWindow() with { Identity = new("Adobe AIR", gameIdentity.ClassName) } }, TimeSpan.Zero) == null);
    Require(ready.Observe(new[] { GameWindow() }, TimeSpan.FromSeconds(1)) == null);
    Require(ready.Observe(new[] { GameWindow() }, TimeSpan.FromSeconds(2)) != null);
});
Sync("physical-pixel coordinate context is restored on success and error", () =>
{
    var contexts = new List<nint>();
    nint Set(nint context) { contexts.Add(context); return new nint(-2); }
    Require(TaskWindowCoordinates.InPhysicalPixels(Set, () => 17) == 17);
    Require(contexts.SequenceEqual(new nint[] { -4, -2 }));
    contexts.Clear();
    try { TaskWindowCoordinates.InPhysicalPixels<int>(Set, () => throw new IOException("read failed")); }
    catch (IOException) { }
    Require(contexts.SequenceEqual(new nint[] { -4, -2 }));
    var accessed = false;
    try { TaskWindowCoordinates.InPhysicalPixels(_ => 0, () => accessed = true); }
    catch (InvalidOperationException) { }
    Require(!accessed);
});
Test("exhausted in-game retries restart login and continue the same uncounted round", async () =>
{
    var confirmedRounds = 2; var runs = 0;
    var lifecycle = new FakeLifecycle();
    var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
        _ => Task.FromResult(++runs == 1 ? new(TaskAttemptStatus.Recoverable, "battle_retry_exhausted") : Good()),
        async (slot, _, token) =>
        {
            Require(confirmedRounds == 2 && runs == 1);
            return await TaskRecoveryPolicy.RestartAsync(lifecycle, slot, token);
        }, default);
    Require(result.Status == TaskAttemptStatus.Success && runs == 2);
    confirmedRounds++;
    Require(confirmedRounds == 3 && string.Join(",", lifecycle.Calls) == "release,kill,start,bind,resize,connect,login");
});
Test("terminal battle return failures may recover but remain bounded even in AFK", async () =>
{
    foreach (var reason in new[] { "battle_return_failed", "task_failed" })
    {
        var restarts = 0;
        var result = await TaskRecoveryPolicy.RunRoundAsync(enabled,
            _ => Task.FromResult(new TaskAttempt(TaskAttemptStatus.Recoverable, reason)),
            (_, _, _) => { restarts++; return Task.FromResult(Ready()); }, default);
        Require(restarts == 3 && result.Status == TaskAttemptStatus.Fatal);
    }
    foreach (var reason in new[] { "battle_retry_exhausted", "battle_return_failed", "task_failed" })
        Require(TaskRecoveryPolicy.ReadCheckpoint(new JsonObject { ["version"] = 1, ["run_id"] = "round",
            ["instance_id"] = "default", ["status"] = "recoverable", ["reason"] = reason }, "round", "default", false)
            .Status == TaskAttemptStatus.Recoverable);
});
var passed = 0;
foreach (var test in tests)
{
    try { await test.Run(); Console.WriteLine("PASS " + test.Name); passed++; }
    catch (Exception error) { Console.Error.WriteLine("FAIL " + test.Name + ": " + error); Environment.ExitCode = 1; }
}
Console.WriteLine($"{passed}/{tests.Count} recovery checks passed; no processes or input devices were used.");

sealed class FakeLifecycle : ITaskRestartLifecycle
{
    public List<string> Calls { get; } = new();
    public string FailAt { get; init; } = "";
    public string CancelAfter { get; init; } = "";
    public CancellationTokenSource? Cancellation { get; init; }
    public int Slot { get; private set; }
    private Task Phase(string name)
    {
        Calls.Add(name);
        if (FailAt == name) throw new IOException(name + " failed");
        if (CancelAfter == name) Cancellation!.Cancel();
        return Task.CompletedTask;
    }
    public Task StopInputsAndAgentAsync(CancellationToken token) => Phase("release");
    public Task StopSelectedProcessAsync(CancellationToken token) => Phase("kill");
    public Task StartSelectedProcessAsync(CancellationToken token) => Phase("start");
    public async Task BindUniqueWindowAsync(CancellationToken token)
    {
        await Phase("bind");
        token.ThrowIfCancellationRequested();
        await Phase("resize");
    }
    public Task ReconnectAsync(CancellationToken token) => Phase("connect");
    public async Task<TaskAttempt> ResumeAsync(int slot, CancellationToken token)
    {
        Slot = slot;
        await Phase("login");
        return new(TaskAttemptStatus.Ready);
    }
}
