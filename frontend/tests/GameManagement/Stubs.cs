// UI-only collaborators: the real service is compiled by the full frontend build.
namespace MFAAvalonia.ViewModels.Pages
{
    public sealed class TaskQueueViewModel { public FakeProcessor Processor { get; } = new(); }
    public sealed class FakeProcessor
    {
        public bool IsTaskRunActive { get; set; }
        public List<string> Logs { get; } = new();
        public void AddLog(string text) => Logs.Add(text);
    }
}
namespace MFAAvalonia.Services
{
    public sealed class GameManagementService
    {
        private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<ViewModels.Pages.FakeProcessor,GameManagementService> Items = new();
        private ViewModels.Pages.FakeProcessor _processor = null!;
        public static GameManagementService For(ViewModels.Pages.FakeProcessor processor) => Items.GetValue(processor,p => new() { _processor = p });
        public GameManagementOptions Options { get; private set; } = new();
        public string SelectedExePath() => Path.GetFullPath("zmBox.exe");
        public void Save(GameManagementOptions options)
        {
            if (_processor.IsTaskRunActive) throw new InvalidOperationException("请先停止当前任务。");
            options.Validate(); Options=options;
        }
        public Task StartAsync() => Task.CompletedTask;
        public Task RequestRestartAsync() => Task.CompletedTask;
        public Task CloseAsync() => Task.CompletedTask;
    }
}
