using System;
using System.Collections.Generic;
using Avalonia.Controls;
using MFAAvalonia.Views.UserControls;

namespace MFAAvalonia.Services;

public static class PageTourCatalog
{
    public static IReadOnlyList<(string Id, string Label)> Pages { get; } = new[]
    {
        ("task_page", "任务页介绍"), ("script_library", "脚本库与录制"),
        ("timeline_editor", "时间轴编辑"), ("version_transfer", "备份与升级")
    };

    public static IReadOnlyList<TutorialStep> Steps(string page, Func<string, Control?> find)
    {
        TutorialStep Step(string id, string title, string text, TipPlacement placement = TipPlacement.Bottom) => new()
        { Id = id, Title = title, Description = () => text, FindTarget = () => find(id), PreferredPlacement = placement };

        return page switch
        {
            "task_page" => new[]
            {
                Step("connection", "连接游戏窗口", "选择你正在使用的造梦盒子窗口，然后连接。连接后核对预览画面，并保持游戏窗口非最小化。"),
                Step("task_list", "选择任务", "勾选任务决定运行哪些内容；点击任务名称查看设置。“＋”可以添加更多可用任务。", TipPlacement.Right),
                Step("task_settings", "调整任务设置", "在这里设置轮数、挂机等选项，并展开“使用说明”查看准备要求。需要战斗脚本的任务，请先到“脚本工具”录制并绑定。", TipPlacement.Left),
                Step("game_management", "管理游戏", "这里可以“启动并连接盒子”、重启或关闭盒子，并设置三项自动维护。展开高级设置可更改盒子绑定；挂机在任务设置中调整。", TipPlacement.Left),
                Step("run_stop", "开始与停止", "准备好后，从这里启动任务队列。需要中止运行时，再点击停止。"),
                Step("logs", "查看运行情况", "这里显示任务进展和停止原因。遇到问题时，先查看最近的提示，也可以导出日志帮助排查。", TipPlacement.Top)
            },
            "script_library" => new[]
            {
                Step("library", "管理脚本", "保存的脚本都在这里，可以打开、复制或管理。自带示例用于了解格式，实际刷关需要适合自己的战斗录制。", TipPlacement.Right),
                Step("recording", "录制操作", "选择窗口并开始录制，倒计时结束前切回游戏。F9 停录，F10 停录并保存；失去焦点会暂停录制。", TipPlacement.Left),
                new TutorialStep { Id = "binding", Title = "绑定任务", FindTarget = () => find("binding"),
                    Description = () => find("binding")?.Name == "ScriptTaskBinding"
                        ? "保存脚本后，在这里指定哪些任务使用它。绑定前请核对任务名称。"
                        : "保存脚本后，每行会提供任务绑定入口。绑定表示这个任务使用哪份脚本。" },
                Step("editor_tab", "编辑录制", "需要调整按键、等待时间或鼠标操作时，可以进入时间轴编辑。")
            },
            "timeline_editor" => new[]
            {
                Step("timeline", "查看录制动作", "这里按时间排列录制动作，可以选择操作并调整位置或时长。", TipPlacement.Top),
                Step("inspector", "修改操作参数", "选中操作后，在这里修改具体参数，并应用更改。", TipPlacement.Left),
                Step("save_undo", "保存与撤销", "修改后记得保存。操作有误时可以撤销；另存为新脚本后，需要重新选择任务绑定。")
            },
            "version_transfer" => new[]
            {
                Step("version", "查看版本", "在这里查看版本和手动升级说明。升级使用完整客户端包，个人备份 ZIP 用于迁移自己的数据。"),
                Step("export_backup", "备份个人数据", "更换电脑或迁移安装位置前，可以导出个人配置与录制脚本备份。"),
                Step("import_backup", "恢复个人数据", "选择个人备份 ZIP 后，先检查恢复预览。确认后的恢复会在下次启动读取配置前处理。")
            },
            _ => Array.Empty<TutorialStep>()
        };
    }
}
