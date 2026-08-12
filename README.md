<!-- markdownlint-disable MD033 MD041 -->
<p align="center">
  <img alt="LOGO" src="https://cdn.jsdelivr.net/gh/Inkyu-Zero/MAAZMXY4@main/docs/images/icon.png" width="256" height="256" />
</p>

<div align="center">

# MAA造梦西游4 (MAAZMXY4)

</div>

基于 **MaaFramework** 的《造梦西游4》自动化脚本，运行在 **造梦盒子** 客户端上。

> 本项目基于 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 项目模板创建。

## ✨ 功能

| 任务 | 说明 |
|---|---|
| **进入游戏** | 自动选择账号 → 进入游戏 → 点击开始游戏 → 选择存档 |
| **刷取灵魂** | 自动刷取灵魂（剑阵+法宝秒杀遁地大盗），支持循环次数、卡死自动重启 |
| **领取奖励** | 进入游戏后依次处理 7 项奖励/活动，每项可单独开关 |

**领取奖励 7 项**：
1. 领取 VIP 每日奖励
2. 领取暑假来就送奖励
3. 进行联盟贡献
4. 冒险领取骰子
5. 领取十五天登录奖励
6. 领取宠物养成并重新采集
7. 领取坐骑养成并重新采集

## 📦 使用

从 **Releases** 下载最新版压缩包，解压后运行 `MAA造梦西游4.exe`，在 **MFA 任务管理器** 中配置：

- **账号名称** / **存档序号**（进入游戏用）
- **循环次数** / **卡死重启**（刷取灵魂用）
- 7 项奖励开关（领取奖励用）

> 游戏需通过 **造梦盒子** 客户端启动，脚本基于窗口截图识别。

## 📁 项目结构

```
assets/
├── interface.json      # 界面配置（任务、选项、版本、GitHub 自动更新）
└── resource/base/      # 流水线(pipeline) + 模板图片(image) + OCR模型(model)
```

> 根目录的 `interface.json` 和 `resource/` 是本仓库 Release 中随应用分发的本地运行副本，不入库；**脚本以 `assets/` 为规范目录**。

## 🔧 开发

- 用 [MaaPE](https://github.com/MaaXYZ/MaaPE) 可视化编辑流水线（`assets/resource/base/pipeline/`）
- 界面配置在 `assets/interface.json`
- 资源校验：`npx @nekosu/maa-tools check`（CI 会自动执行）

## 📄 版本

- **v0.3** — 当前版本

## 🙏 感谢

- [MaaFramework](https://github.com/MaaXYZ/MaaFramework)
- [MFAAvalonia](https://github.com/MaaXYZ/MFAAvalonia)
- [MaaPE](https://github.com/MaaXYZ/MaaPE)
