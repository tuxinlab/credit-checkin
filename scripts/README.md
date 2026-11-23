# Windows 计划任务安装脚本（个人使用）

以**当前登录用户**注册一个每天 10:20 运行的计划任务，调用本项目的 `run` 命令。

> 注意：任务在登录用户的普通权限下运行。请在已登录目标平台客户端/已注入凭据的用户账户下执行本脚本。

## 安装

以普通 PowerShell（无需管理员）在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows.ps1
```

脚本会：

1. 探测 `python`（或 `py -3`）与项目根目录；
2. 注册任务 `CreditCheckinDaily`，每天 10:20 触发；
3. 设置 `StartWhenAvailable`（错过后开机补跑）、`ExecutionTimeLimit = 20 分钟`；
4. 输出下次运行时间。

## 查看 / 手动运行 / 卸载

```powershell
Get-ScheduledTask -TaskName "CreditCheckinDaily"
Start-ScheduledTask -TaskName "CreditCheckinDaily"
Unregister-ScheduledTask -TaskName "CreditCheckinDaily" -Confirm:$false
```

## 说明

- 任务只调用 `python -m checkin run`，按日去重由程序内部完成。开机补跑（`StartWhenAvailable`）只在**当天**有效；平台活动按天结算，跨天的积分补不回来，本项目不做跨天补签。
- 本脚本只注册一个 10:20 触发点。若想增加可配置的兜底时间（默认 15:00），推荐改用 Web 面板的「每日自动签到」来注册/管理任务，或在脚本里自行增加第二个 `New-ScheduledTaskTrigger -Daily -At '15:00'`。
- 如需守护模式（窗口内多轮轮询），不要安装本任务，改用 `python -m checkin schedule --interval-minutes 30`。
- 场景差异（如 Qoder 10:00 开窗）可在 `config.json` 的 `checkin_window` 与任务时间上自行调整。
