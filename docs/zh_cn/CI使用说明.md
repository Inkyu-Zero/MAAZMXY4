# CI（GitHub Actions）使用说明

本文档面向本项目维护者，详细解释 **GitHub Actions 的概念**、**`gh` 常用命令**，
以及本仓库三个工作流（workflow）的作用与排查方法。

---

## 一、先弄清几个概念

CI 的术语容易混，先对齐：

| 概念 | 含义 | 在 GitHub 上的位置 |
| --- | --- | --- |
| **Workflow（工作流）** | 一个 `.github/workflows/*.yml` 文件，描述"什么时候、做什么" | 仓库 → Actions → 左侧列表 |
| **Run（运行）** | 工作流的一次执行，有唯一 `run-id` | Actions 里的一行记录 |
| **Job（作业）** | 一次运行里的一个任务，默认并行；作业之间可以有依赖（`needs`） | 运行详情页的一个分组 |
| **Step（步骤）** | 作业里的一步，按顺序执行；任一步失败则整个作业失败 | 作业里的一行（带 ✓/✗） |
| **Artifact（构建产物）** | 运行过程中上传的文件，可从网页或 `gh` 下载，**有保留期限** | 运行详情页底部 Artifacts |
| **Release（发布）** | 挂在某个 tag 上的发布页，可带附件供用户下载 | 仓库 → Releases |

**关键区别**：

- **artifact** 是给**你**调试用的，会过期、需要登录才能下载
- **release 附件** 是给**用户**下载的，永久保存、公开可下

本项目的完整版包同时做这两件事：CI 内部先传 artifact（方便检查），打 tag 时再传到 release。

---

## 二、本项目的三个工作流

| 文件 | 触发条件 | 作用 |
| --- | --- | --- |
| `check.yml` | push（改动 `assets/**`、`**.py`、自身）或 PR | 跑 maa-checker + JSON Schema 校验 |
| `release-adventure.yml` | 打 `v*` tag 或手动 | 打包 **slim 脚本包**（约 40 KB） |
| `release-adventure-full.yml` | 打 `v*` tag 或手动 | 打包 **完整版**（约 195 MB） |

> 另有 `install.yml`、`mirrorchyan_*.yml`、`sync_schema_files.yml` 是项目原有的，
> 与生存大冒险无关。

### release-adventure-full.yml 都做了什么

这是本项目里最复杂的一个，逐步说明：

1. **Checkout** —— 拉取仓库代码
2. **Setup Python + Install MaaFw** —— 准备跑测试的环境
3. **Run offline tests** —— 跑 `tools/test_agent_offline.py`（40 项断言），**失败即中断**
4. **Check pipeline structure** —— 跑 `tools/check_adventure.py`
5. **Resolve base tag** —— 决定用哪个版本作"基准包"（默认 `v1.1`）
6. **Download base full package** —— 从该 Release 下载完整版 zip（约 195 MB）
7. **Extract base** —— 解压，并按 `MAA*.exe` 的位置定位根目录
8. **Build slim package first** —— 跑 `tools/build_release.py` 生成 slim 包
9. **Overlay new scripts** —— 用 slim 包里的新脚本覆盖基准包
10. **Patch interface.json** —— 合并任务与 agent 配置（**不整体覆盖**，避免丢掉你原有的任务）
11. **Repack full** —— 重新打包，排除 logs/debug/temp/config/`__pycache__`
12. **Verify package content** —— 校验必备文件、`python.exe`、`site-packages/maa` 都在，缺一即失败
13. **Upload artifact** —— 上传供下载检查
14. **Publish GitHub Release** —— **仅在 tag 触发时执行**（手动触发会跳过）

---

## 三、命令详解

以下命令都在仓库目录里执行（`cd C:\Users\k1508\Desktop\Maa\MAA造梦西游4`）。
不在该目录时，给每条命令加 `-R Inkyu-Zero/MAAZMXY4`。

### 3.1 查看

#### `gh workflow list` —— 列出所有工作流

```powershell
gh workflow list
```

输出三列：名称、状态（`active`/`disabled_manually`）、ID。ID 在 `gh workflow run` 时可用。

#### `gh run list` —— 列出最近的运行

```powershell
gh run list --limit 10
```

输出列的含义：

```
completed  success  fix: 修掉 CI 的两处既有失败   check   main   push   36933777671   25s   2026-10-01T22:13:54Z
   ↑          ↑              ↑                    ↑       ↑      ↑         ↑          ↑          ↑
 状态       结论        触发的提交标题          工作流   分支   事件     run-id     耗时      时间
```

- **状态**：`queued`（排队）→ `in_progress`（跑着）→ `completed`（结束）
- **结论**：`success` / `failure` / `cancelled` / `skipped`
- **事件**：`push`（推代码）/ `workflow_dispatch`（手动触发）/ `pull_request`

常用过滤：

```powershell
gh run list --status failure          # 只看失败的
gh run list --status in_progress      # 只看正在跑的
gh run list --workflow check.yml      # 只看某个工作流
gh run list --branch main --limit 5   # 只看 main 分支
```

#### `gh run view <run-id>` —— 看某次运行的详情

```powershell
gh run view 36933777671
```

实际输出形如：

```
✓ main check · 36933777671
Triggered via push about 5 minutes ago

JOBS
✓ resource in 22s (ID 110609117653)

View this run on GitHub: https://github.com/Inkyu-Zero/MAAZMXY4/actions/runs/36933777671
```

加上 `--verbose` 会展开每个步骤：

```powershell
gh run view 36933777671 --verbose
```

#### `gh workflow view` —— 看某个工作流的概览

```powershell
gh workflow view release-adventure-full.yml
gh workflow view release-adventure-full.yml --yaml   # 直接看它的 YAML
```

#### `gh release view` —— 看某个发布

```powershell
gh release view v1.1
```

输出包含标题、tag、是否草稿、附件列表。想拿结构化数据（尤其文件名带中文时，控制台可能显示不全）：

```powershell
gh release view v1.1 --json name,tagName,assets,url
```

### 3.2 触发

#### `gh workflow run` —— 手动触发

```powershell
gh workflow run release-adventure-full.yml
```

参数：

| 参数 | 作用 |
| --- | --- |
| `-f key=value` | 传字符串输入（raw，不做类型解析） |
| `-F key=value` | 传输入并**按类型解析**（数字、布尔） |
| `--ref <branch/tag>` | 指定用哪个分支/标签上的 workflow 文件 |
| `--json` | 从标准输入读 JSON 作为输入 |
| `-R owner/repo` | 指定仓库 |

本项目可传的输入：

```powershell
# 完整版：指定用哪个 Release 作基准
gh workflow run release-adventure-full.yml -f base_tag=v1.1

# 也可指定要发布的版本号（手动触发时用；不填则用当前 ref）
gh workflow run release-adventure-full.yml -f asset_tag=v1.2
```

> **手动触发不会创建 Release**。workflow 里的发布步骤带了
> `if: startsWith(github.ref, 'refs/tags/')`，只有 tag 触发时才执行。
> 所以可以放心用手动触发来测试。

#### 打 tag 触发（正式发布）

```powershell
git tag v1.2
git push origin v1.2
```

**tag 名必须以 `v` 开头**，否则不匹配 workflow 里的 `tags: ["v*"]`。

删掉打错的 tag：

```powershell
git tag -d v1.2                    # 删本地
git push origin :refs/tags/v1.2    # 删远程（注意冒号前是空的）
```

### 3.3 看日志

```powershell
# 只看失败步骤（输出短，排查首选）
gh run view 36933777671 --log-failed

# 完整日志（很长，建议重定向到文件）
gh run view 36933777671 --log > ci.log

# 实时盯着跑，结束自动退出
gh run watch 36933777671

# 只看某个作业
gh run view 36933777671 --job build-full --log
```

**日志读法**：每行前缀是 `作业名  步骤名  时间戳 内容`。看 `##[error]` 开头的行就是失败点。

### 3.4 下载产物

```powershell
# 下载某次运行的全部 artifact
gh run download 36933353252

# 只下载指定名字的
gh run download 36933353252 -n survival-adventure-full

# 用通配符匹配
gh run download 36933353252 -p "survival-*"

# 指定下载目录
gh run download 36933353252 -D .\downloads

# 不写 run-id：下载最近一次运行的全部 artifact
gh run download
```

> artifact 的保留期由 workflow 里的 `retention-days` 决定，本项目完整版设的是 **7 天**。
> 过期后只能重新跑一次。

### 3.5 运维

```powershell
# 重跑整次运行
gh run rerun 36932946194

# 只重跑失败的作业
gh run rerun 36932946194 --failed

# 取消正在跑的
gh run cancel 36933353252

# 删除一条运行记录
gh run delete 36933353252

# 临时禁用/启用工作流（例如不想让 check 每次 push 都跑）
gh workflow disable check.yml
gh workflow enable check.yml
```

### 3.6 Release 管理

```powershell
gh release list                                  # 列出所有发布
gh release view v1.1                             # 看详情
gh release download v1.1 --pattern "*slim*"      # 只下载某个附件
gh release download v1.1 -D .\rel                # 全部下载到目录
gh release delete v1.2 --yes --cleanup-tag       # 删发布并连带删 tag
```

手动发一个（CI 出问题时的兜底）：

```powershell
gh release create v1.2 .\dist\*.zip `
  --title "v1.2" `
  --notes-file release_notes.md
```

---

## 四、失败排查手册

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `Resource not accessible by integration` | workflow 缺 `permissions` | 在 workflow 顶层加 `permissions: contents: write` |
| `Validation failed for interface.json` | schema 违规 | 本地跑 `python tools/validate_schema.py ...` 看具体哪条 |
| `⚠ 缺失 xxx`（自己加的校验） | 打包内容不全 | 看该步骤上方 `ls` 的输出，确认 overlay 是否成功 |
| `Process completed with exit code 1` | 某步骤返回非零 | 用 `--log-failed` 定位到**具体哪个步骤** |
| `Node.js 20 is deprecated` | 只是**警告**，不影响结果 | 忽略；后续可把 `actions/*` 升到 v5 |
| 找不到 artifact | 过期了（默认 7 天） | 重新触发一次 |

**排查顺序建议**：

1. `gh run list --status failure` 找到失败的运行
2. `gh run view <id> --log-failed` 看报错原文
3. 如果是**测试失败**，本地复现：`python tools/test_agent_offline.py`
4. 如果是**打包失败**，本地复现：`python tools/build_release.py --version 1.2 --out dist`

---

## 五、发一个版本的完整流程

```powershell
# ① 改完代码，本地自查（省一次失败等待）
python tools/test_agent_offline.py
python tools/check_adventure.py

# ② 提交推送
git add -A
git commit -m "feat: xxx"
git push

# ③ 打 tag 发布（两个工作流会自动跑）
git tag v1.2
git push origin v1.2

# ④ 盯进度（可选，跑完自动退出）
gh run watch

# ⑤ 确认结果
gh release view v1.2
```

第 ③ 步之后全自动：e2e 测试 → schema 校验 → 打包 slim → 下载基准包 →
组装完整版 → 上传两个附件。大约 **1 分钟（slim）+ 1 分钟（full）**。

---

## 六、本地能做哪些 CI 会做的事

CI 要跑的每一步，本地基本都能先跑一遍：

| CI 步骤 | 本地等价命令 |
| --- | --- |
| 离线测试 | `python tools/test_agent_offline.py` |
| 结构自检 | `python tools/check_adventure.py` |
| schema 校验 | `python tools/validate_schema.py --schema-dir deps/tools --resource-dirs assets/resource --exclude-dirs assets/resource/announcement --interface-files assets/interface.json` |
| 打包 slim | `python tools/build_release.py --version 1.2 --out dist` |
| maa-checker | `npm ci && npx @nekosu/maa-tools check` |

> **完整版无法在本地一键构建**——它需要 MAA 本体与 Python 运行时，这些不在仓库里。
> 要么走 CI，要么手动用你的整合包做基准自行替换。
