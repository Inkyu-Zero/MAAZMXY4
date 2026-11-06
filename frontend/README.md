# 脚本工具前端

> 自 2.4.2 起通过完整客户端手动升级。2.4.3 增加个人配置与脚本 ZIP 备份/恢复；2.4.5 增加原地放弃并重录、窗口调整和通用脚本播放。版本状态与早期炼丹安装包的依赖问题见[项目 README](../README.md#版本状态)。

“版本与手动升级”页面支持个人配置与录制脚本的 ZIP 导出/导入。压缩与解压集成在助手中，使用其现有 .NET 运行时，无需外部程序；校验、预览并确认后，在下次启动读取配置之前备份及恢复。操作范围、覆盖规则和离线验证见[个人配置备份与迁移](../docs/zh_cn/个人配置备份与迁移.md)。

2.4.1-rc.1 增加按任务保存的挂机勾选项，并补齐重启后的窗口刷新、加载等待及八存档核对。测试范围和上游 2.4.0 旧前端混装的分析见 [2.4.1-rc.1 更新说明](../docs/zh_cn/2.4.1-rc.1更新说明.md)。完整包构建现在核对前端源码、补丁集合与程序集哈希，不能用资源版本号代替前端验收。

本目录为现有 MFAAvalonia 增加原生“脚本工具”页面，沿用 Avalonia、SukiUI 和任务前端。页面内采用两个子标签：“脚本库与录制”和“时间轴编辑”。录制和编辑规则由 ZaoMengScript 迁移，Python 服务不依赖 Qt。此次交互更新及验收记录见 [更新记录](UX-CHANGELOG.md)。

用户操作说明见 [脚本工具使用说明](../docs/zh_cn/脚本工具-使用说明.md)。

完整客户端提供原生“游戏百科”侧栏页，数据从 `agent/data/game_database/` 离线读取。相关来源、布局、开发工具与验收边界见[游戏数据库与导航素材](../docs/zh_cn/游戏数据库与导航素材.md)。前端与相应资源需一起安装，单独更新资源不会给旧前端增加页签。

2.4.5 在录制区和未保存录制的时间轴上方提供“放弃本次录制”，复用同一服务操作。录制区可勾选“双击放弃本次录制”防止误触；选项仅影响录制区按钮，在当前页面有效，默认不勾选。放弃后清空本次未保存内容并释放录制监听；在录制区操作时不切换页面，保留窗口选择和倒计时，可直接重新开始。已保存脚本和绑定不变。

## 源码与构建

`upstream.json` 固定 MFAAvalonia 提交、源码归档 SHA-256 和构建入口，依赖版本以该清单和固定源码为准。`ScriptStudio/` 内的文件按 MFAAvalonia Core 项目目录布局存放。构建脚本将其复制到下载源码的 `MFAAvalonia/` 下，纯 C# `UserControl` 也可使用，不要求额外的 `.axaml` 文件。

需要 .NET 10 SDK；仅安装 .NET Runtime 无法编译。脚本优先使用 `.runtime-cache/dotnet-sdk/dotnet.exe`，也可通过 `-DotnetPath` 指定。

```powershell
# 下载、校验固定源码，应用补丁并复制页面源码，不编译。
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build_frontend.ps1 -PrepareOnly

# 构建到 dist/script-studio，不修改正在使用的前端。
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build_frontend.ps1

# 首次还原成功后，重复编译可以省去依赖还原。
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build_frontend.ps1 -NoRestore

# 关闭本项目的 MFAAvalonia 后安装；脚本先备份将被覆盖的文件。
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build_frontend.ps1 -Install
```

源码归档和解压目录位于 `.runtime-cache/`，编译产物位于 `dist/script-studio/`，均不提交 Git。默认构建产物是前端，不包含游戏资源项目、录制文件和内嵌 Python；运行时应与项目发布目录组合。`-Install` 只安装前端程序、托管依赖和对应 Win32 原生依赖，不覆盖 `interface.json`、`resource/`、`agent/`、`config/`、`recordings/` 或 Python 目录。

补丁目标每次从通过 SHA-256 校验的上游归档重新读取，按清单顺序在内存中组合全部补丁，全部匹配成功后才写回缓存。因此旧补丁、重复嵌套或中断留下的目标文件都会恢复到与干净构建一致的内容；未变化的目标文件保留时间戳，`bin/obj` 等编译缓存不清理。缓存中的 `.script-studio-patch-targets.json` 记录本构建流程管理的目标，移除补丁时也会恢复对应上游文件。自定义代码应写入源码覆盖层，不应直接修改生成缓存。

构建记录包含 `tools/build_frontend.ps1` 的源码哈希及实际生成目标的哈希。完整包工具会拒绝缺少或不匹配构建脚本哈希的旧前端，避免相同资源版本掩盖旧构建流程的问题。升级构建工具后须重新构建前端；这不要求修改或安装日常客户端。离线缓存回归入口为 `tests/test_frontend_patch_reapply.py`。

`upstream.json` 的 `application_icon` 指向仓库根目录 `新图标.ico`。构建时统一替换宿主的 `Assets/logo.ico`，供 Windows EXE 和 Avalonia 内嵌窗口/托盘图标使用；构建记录包含图标 SHA-256，不需要手工修改缓存源码。

源码与日常客户端分开存放时，可使用 `-Install -InstallDirectory <现有客户端目录>`。仍从当前源码树构建，在目标客户端目录检查运行状态、保存安装备份并安装上述前端文件；构建输出必须放在该目标目录之外。资源和 Agent 需另按既有迁移流程同步。

新前端启动未绑定的脚本任务时，只在日志提示绑定方式并走宿主 `STOPPED` 路径，结束本次队列；不弹选择框、不执行回放、不计成功次数或继续重复。已绑定但文件丢失/损坏仍作为实际错误处理。协议通过 `report_unbound` 显式请求正常停止结果，旧调用仍保留严格校验。

安装备份位于 `backup/frontend-script-studio-日期-随机后缀/`。其中 `installation.json` 记录覆盖前已存在和新建的文件；需要恢复时，关闭前端，将备份文件按相同相对路径复制回项目。脚本不会主动关闭任何现有 MFAAvalonia 进程。

## 打包与手动升级

当前源码已移除 `updater.py`、`update_engine/` 及项目自动更新流程，交付完整 EXE 和 ZIP 后由用户手动安装。CI 的 `script-studio-frontend-win-x64` 只是前端附件，不含 Python 和全部项目资源，不能当作完整客户端分发。

完整客户端的标准入口为 `MAA造梦西游4.exe`，直接启动同目录的 `MFAAvalonia.exe`。Python 用于 Agent 和脚本服务。发布验收需验证标准启动入口，不能只测试某个程序集或脚本能运行。

完整包保留空的 `runtimes/win-x64/native/plugins/` 目录，供 MaaFramework 按其原有机制查找可选插件；该目录可以为空，不需要添加假插件。缺少目录时框架可能把目录路径当作动态库并输出加载错误。

启动器从 `tools/launcher/Program.cs` 构建。`tools/build_launcher.ps1` 使用 Windows .NET Framework 编译器及项目图标，生成 EXE 和 `launcher-build.json`，打包核对源码、图标及二进制摘要。Inno Setup 安装 EXE 与安装后的项目启动器是不同文件，不可互相替代。

完整客户端还应包含 `script-studio-build.json`、`LICENSE-MFAAvalonia.txt`、`agent/recordings/` 和 `agent/task_asset_sync.py`，不得夹带发布者的 `recordings/`、`config/`、个人绑定、日志或缓存。根目录 `recordings/` 是用户库，与 `agent/recordings/` 发布模板分开；默认模板首次入库后不覆盖用户编辑，不恢复用户删除。

正式打包前先统一资源、启动器和发布标签的版本，以下示例读取当前资源版本。整包从规范源码取资源，不混入日常运行副本：

```powershell
./tools/build_frontend.ps1
$version = (Get-Content assets/interface.json -Raw | ConvertFrom-Json).version
./tools/build_launcher.ps1 -Version $version
python tools/runtime_check.py
python tools/build_release.py --client --launcher "dist/launcher/MAA造梦西游4.exe" --frontend "<编译输出目录>" --python-runtime "<完整内嵌Python目录>" --repository "<发布者>/<仓库>"
python tools/build_release.py --verify-client "<完整包解压目录>"
```

输出 `dist/MAAZMXY4-v<资源版本>-win-x64.zip`，不会覆盖已有同名包。`--frontend` 可指定 CI 前端附件的解压目录。启动器、前端、完整内嵌 Python、项目资源及依赖安装 BAT 均被显式收集；个人配置、录制和日志不作为公共内容打包。`--verify-client` 检查解压后的启动器、必需依赖文件、公共任务及录制返回确认节点，不能替代下方的实际功能探针和启动验收。`runtime_check.py` 默认检查源码树下的 `runtimes/python/`；若打包使用另一个运行时，必须另外检查实际输入及最终包。

单文件 EXE 使用 Inno Setup 图形安装器，输入仍是同一份已验证完整 ZIP：

```powershell
python -B tools/build_installer.py --client-zip "<完整客户端.zip>" --compiler "<Inno Setup 7.1.1/ISCC.exe>" --cache "<依赖缓存>" --download --out "<新输出-setup.exe>" --work "<新的构建工作目录>"
```

固定工具和依赖清单在 `tools/installer/dependencies.json`。构建器检查编译器和依赖 EXE 的 SHA-256、Authenticode 状态与发布者；下载地址更新但摘要不符时失败，不自动信任新版二进制。当前前端要求 `Microsoft.NETCore.App 10.0.0`，已有正式 10.0 补丁版本即可运行；不额外要求 Desktop Runtime。VC++ 按清单最低版本检测，较新版本不降级。安装时再次核对包内依赖摘要，缺失才补装；失败或取消权限提示时停止，3010 表示成功但需要重启。原始依赖包及许可链接随包记录，不在安装时联网下载。

新目录自动追加 `MAAZMXY4`，已确认的旧客户端原位保留个人数据并清理旧程序，不产生旧程序备份；存在其它文件不直接拒绝安装。识别不确定时采用子目录安装，不能据文件夹名称删除内容。原配置目录、录制、绑定、草稿、个人布局和未知根目录文件保留；旧任务选择通过现有迁移规划转换，规划失败先停止清理。安装器建立卸载登记和可选快捷方式，不自动启动客户端或结束正在运行的进程；卸载保留个人数据，不卸载系统共享运行库。

`tools/build_sfx.py` 仅供复现历史自解压资产；新 EXE 不再依赖旧官方 EXE 前缀。安装器仅是完整客户端手动安装入口，不恢复自动更新。构建输出附 `.build.json`，记录客户端 ZIP、安装器、依赖与安装脚本摘要。发布仍需在独立目录实际安装、核验包内依赖及获准启动验收。

“版本与手动升级”页提供版本说明和个人 ZIP 迁移，不安装完整客户端或自动更新程序。通用 MFA 自动更新保持禁用，以免覆盖定制页面。个人配置备份 ZIP 与完整客户端 ZIP 不同：前者只能通过迁移页面恢复，后者按完整安装流程解压。

手动升级的迁移命令与文件覆盖顺序见[使用说明](../docs/zh_cn/脚本工具-使用说明.md#手动升级客户端)。必须在替换旧 `interface.json` 前迁移，单纯保留 `config/` 和录制文件不等于保留旧任务的脚本选择。

发布前应在隔离目录验证完整客户端；fork 试发布后重新下载实际 Release 资产，检查首次安装、旧版手动升级、启动器及用户数据。最终包的哈希、实际安装和远端 CI 各自取证，不以前一版本候选包的测试结果代替。是否提交上游 PR 由发布者决定。

## 发布前依赖检查

运行时收集使用 `tools/setup_runtime.py`，必需文件与基础原生调用检查使用 `tools/runtime_check.py`。2.4.5 已将 OpenCV 的 `cv2` 包、本机扩展和分发元数据加入收集与检查；缺少依赖时应阻止打包，不以任务能显示或模块能导入代替验证。

对候选包及实际公开下载的 EXE、ZIP，分别安装到独立目录，核对版本、哈希、文件清单和依赖来源。必须使用包内解释器，禁止向搜索路径追加系统 Python、研究环境或旧客户端的依赖。检查延迟导入和异常被静默忽略的路径，并用合成数据执行代表性函数；例如在**待验证客户端根目录**运行以下炼丹探针：

```powershell
@'
from pathlib import Path
import sys
import numpy as np
import PIL
import cv2

root = Path.cwd().resolve()
sys.path.insert(0, str(root / 'agent'))
from jzld import board
for module in (np, PIL, cv2, board):
    path = Path(module.__file__).resolve()
    assert path.is_relative_to(root), str(path)
    print(module.__name__, path)
image = np.arange(25, dtype=np.float32).reshape(5, 5)
assert board._best_ncc(image, image) > .99
assert np.isfinite(cv2.Sobel(image, cv2.CV_32F, 1, 0, ksize=3)).all()
assert len(board._templates()) == 9
assert board._empty_template() is not None
assert board._slag_template() is not None
print('PASS', sys.version.split()[0], np.__version__, PIL.__version__, cv2.__version__)
'@ | .\runtimes\python\python.exe -B -E -s -X utf8 -
```

该探针不连接游戏、不截图、不发送输入；通过只代表这些依赖和离线函数可用，不代表已完成真实炼丹验收。正式验证还需按授权范围检查标准启动器及相关功能。

对比上一正式版和最终包的文件清单、压缩前后大小，逐项解释依赖删减。2.4.3 早期包的体积缩小主要来自遗漏 `cv2`，炼丹源码本身未丢；仅比对源码或加载 Pillow 模板不能发现这一错误。缺依赖、ABI/DLL 加载失败、模块来源越界或功能探针失败均阻止发布，保留失败与修复后的证据。同名版本的附件若发生变化，应重新下载检查，不能沿用旧资产的结果。

## 宿主接入

`patches/` 使用精确文本补丁，每个补丁包含目标路径、旧文本和新文本。脚本从校验归档开始按清单顺序组合补丁，要求每一步的旧文本只出现一次；生成内容未变化时不重写文件，锚点变化时停止，不尝试模糊修改其它版本源码。

- `01-script-studio-navigation.json`：在共享 `RootViewContent.axaml` 的现有侧栏加入页面。
- `02-script-studio-registration.json`：接入现有 DI 和 ViewLocator 映射。
- `03-script-studio-task-guard.json`：在两个任务启动入口共用的 `TryBeginTaskRun()` 内，与录制端使用同一把锁，防止录制、盒子重连与界面、定时或其它实例启动任务并行。冲突通过现有日志反馈。
- `04-selected-win32-controller.json`、`05-selected-controller-resource.json`：有多个 Win32 配置时，窗口、资源及任务使用当前命名控制器的筛选规则，支持同类型控制器使用不同窗口与资源设置。
- `06-task-script-execution.json`：在现有 MFA 任务队列入口查询通用任务绑定，保留任务内回放或明确替代整个任务；支持配置声明的有界恢复，失败与取消停止后续重复。`07-win32-recovery-rebind.json` 在重启后绑定经过校验的唯一窗口，避免旧设备变更回调与显式重连竞态。
- `08-task-stop-race.json`：停止操作捕获同一执行器引用，避免恢复释放执行器时出现空引用竞态。
- `09-agent-callbacks-only.json`、`10-agent-connection-health.json`：实时预览使用仅资源控制器绑定，并在运行前检查闲置 Agent 连接，避免截图事件堵塞任务启动。
- `11-script-studio-icons.json`：通过现有 Avalonia 资源系统加载 imagegen 生成的工具栏图集，提示词与 PNG 同目录保存。
- `12-option-default-array.json`、`13-option-default-string.json`：字符串形式的选项先保留未选择状态，由现有 TaskLoader 应用 `default_case`，修复默认值总落到第一项的问题。已有配置对象的明确索引保持不变。
- `14-shared-recording-windows.json`：录制页复用当前任务页的窗口发现、控制器筛选与已选窗口顺序，适配通用 Win32 控制器；刷新只读窗口信息，不连接或切换设备。
- `15-manual-only-updates.json`、`16-manual-only-updates.json`、`25`—`28` 的 `manual-only-updates` 补丁：移除通用软件、资源和框架自动更新入口，改为项目手动升级说明，避免覆盖定制前端。
- `58-personal-transfer-startup.json`：在宿主读取配置前处理已确认的个人 ZIP 导入，备份旧内容并在失败时回滚。
- `36-game-management-card.json`、`37-task-description-in-settings.json`：游戏管理替换原任务说明卡片，原 Markdown 说明控件移入任务设置，保留布局标识和绑定。
- `38-disable-first-use-tutorial.json`：以功能开关暂时跳过首次指引，不改写用户的完成标记。
- `39-game-management-lifetime.json`、`40-game-reconnect-preview.json`：实例释放时结束监测，重连期间沿用实时预览的连接暂停机制。
- `48`—`52`：移除任务页装饰脉冲、光带和更新提示的无限渐变；运行图标只在自身可见时动画。实例下拉列表的子图标也绑定运行状态，避免只隐藏父容器后动画仍然计时。保留静态状态提示和运行中的指示器，不改渲染后端或全局帧率。

游戏管理使用每实例设置与低频进程采样，原生任务队列继续持有输入；自动维护、同轮恢复和答题整局边界见[游戏管理说明](../docs/zh_cn/游戏管理.md)。`TaskScriptPlan.PrepareManaged` 和任务目录发现器保留原计次所有权，不另建输入执行器。`frontend/tests/GameManagement/` 用真实控件及替身服务检查布局和设置，不启动任何游戏进程。

默认录制包位于 `agent/recordings`。`recording_store.py` 负责首次入库、默认标记、复制、带恢复副本的删除与绑定原子更新；编辑和回放仍使用原有录制模型及文件。`agent/task-migrations.json` 声明退役入口，完整任务同步只移除名称和入口均匹配的旧条目，无关自定义任务保留。

任务目录由 `agent/task_catalog.py` 从现有 `interface.json`、导入定义与实例任务发现。普通任务标识由名称和入口稳定生成，支持 Pipeline v1/v2 的 `battle_play` 节点；旧绑定标识可在任务初始化参数的 `execution.binding_id` 中声明兼容。UI 和 C# 不内置副本白名单。

复杂任务通过 `script_task_state`、`execution.resume_entry`、`execution.settings_node`、`playback_node`、`routes` 和恢复入口的 `resume_slots` 声明运行与恢复能力。识别节点仍由各任务 Pipeline 定义。任务查询复用现有脚本服务协议，执行仍使用 Maa 控制器和任务队列。任务资源安装使用 `tools/sync_task_assets.py --task <任务名> --apply`，默认只预览。

宿主互斥接口由页面源码中的 `MFAAvalonia.Services.ScriptStudioGuard` 提供：`SyncRoot`、`IsCapturing` 和 `ThrowIfCapturing()`。录制端必须在同一把锁内检查所有实例的任务状态并取得录制所有权，停止及异常时释放。

上游源自 [MaaXYZ/MFAAvalonia](https://github.com/MaaXYZ/MFAAvalonia/tree/1080d3e1500f3439ac896ab2eca9aeb8e885f1f4)，保留上游依赖版本，未对其进行无关升级。构建产物附带 `LICENSE-MFAAvalonia.txt` 和 `script-studio-build.json`，用于记录上游许可与源码版本；本目录的补丁与页面源码随本工程提供。

玩家界面统一字体与信息层级；百科优先显示获取方式、用途与技能，技术编号不在阅读区展示，属性参考和来源按需展开。时间轴支持框选、批量移动/删除及单次撤销。任务设置把旧版无限次数归入挂机模式，游戏内重试与盒子重启分别执行。相关回归包含 `frontend/tests/TaskOptions`（在前端构建后读取实际程序集）和 `tests/test_timeline_batch.py`。
