# 配置说明（config.json）

由 `python -m checkin init` 生成模板。文件为 JSON，支持用 `${VAR}` 引用环境变量（例如 webhook 地址）。

## 顶层字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `timezone` | `Asia/Shanghai` | 日期判定与计划任务使用的时区 |
| `checkin_window` | `09:00 ~ 12:00` | 守护模式 `schedule` 的执行窗口（含端点，支持跨零点） |
| `catchup_days` | `3` | **兼容保留字段，已不起作用**。平台活动按天结算，不做跨天补签；漏签保障靠当天两次触发 + 开机补跑 + 失败重试 |
| `retention_days` | `180` | 历史记录保留天数；`0` 表示永不清理 |
| `schedule` | 见下 | 面板「每日自动签到」使用的定时任务设置 |
| `retry.max_attempts` | `3` | 单任务最大尝试次数（含首次） |
| `retry.backoff_seconds` | `[60,300,900]` | 每次失败后的等待秒数 |
| `notify.channels` | `["console","log"]` | 可加 `"webhook"` |
| `notify.webhook` | 空 | Webhook URL（POST JSON），凭据同样脱敏 |
| `notify.on_failure` / `notify.on_drift` | `true` | 是否在失败/漂移时推送 |
| `contracts_dir` | `contracts` | 契约目录（相对项目根） |
| `database` | `data/checkin.db` | SQLite 数据库 |
| `log_file` | `logs/checkin.log` | 运行日志 |
| `platforms` | 见模板 | 平台 → 版本 → 账号列表 |

`schedule` 由 Web 面板写入，字段含义：

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `false` | 是否注册了每日计划任务 |
| `time` | `10:20` | 主触发时间 |
| `second_time` | `15:00` | 可选兜底触发时间；为空则只跑主时间一次 |
| `task_name` | `CreditCheckinDaily` | Windows 计划任务名 |

## 平台/版本/账号

结构固定为 `platforms.<platform>.versions.<version>.accounts[]`。

- `platform`：`workbuddy` / `trae` / `qoder`
- `version`：`cn`（国内版）/ `global`（国际版）
- 每层都有 `enabled`，任一层为 `false` 时其下账号不参与计划。
- 账号字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 全局唯一，作为去重键的一部分 |
| `name` | 否 | 备注名，仅用于展示 |
| `enabled` | 否 | 单账号开关 |
| `credential_env` | 二选一 | **推荐**。环境变量名，凭据从环境变量读取 |
| `credential` | 二选一 | 行内凭据，强烈不建议；如使用请不要提交文件 |
| `metadata` | 否 | 扩展字段，留给专用适配器（如 token 端点） |

示例：

```json
{
  "id": "wb-cn-01",
  "name": "主号",
  "enabled": true,
  "credential_env": "WORKBUDDY_CN_TOKEN_1"
}
```

## 凭据安全

凭据解析优先级：**环境变量 / 行内凭据 → 本地保险库 → 本机客户端登录态**。环境变量优先于面板保存的值；想让页面配置生效，请确保对应环境变量为空。

1. 不要把 token / PAT / refreshToken 提交到 Git。`config.json`、`data/`、`logs/` 均已在 `.gitignore` 中排除。
2. **推荐**：在 Web 面板点「配置凭据 → 自动获取本机凭据」（或 Qoder 的「设备授权登录」），token 经 Windows DPAPI 加密后保存到 `data/vault.json`，页面只显示脱敏值。
3. **手动方式**：用环境变量注入，例如 Windows PowerShell：

   ```powershell
   $env:WORKBUDDY_CN_TOKEN_1 = "xxxxx"
   $env:QODER_CN_PAT_1 = "pt-xxxxx"
   python -m checkin run
   ```

4. `python -m checkin doctor` / `probe` 只显示来源与脱敏值，不输出 token 原文。
5. 日志、历史明细、漂移样本与所有 API 返回中的敏感字段会自动替换为 `<redacted>`。

## 真实签到所需的环境变量

凭据提供器按 `平台_字段` 读取环境变量（也可用 `<平台>_EXTRA_HEADERS` 注入任意头，值为 JSON）：

| 环境变量 | 用途 |
|---|---|
| `WORKBUDDY_ACCESS_TOKEN` | 国内/国际版 Bearer token（从客户端登录态导出） |
| `WORKBUDDY_UID` / `WORKBUDDY_DOMAIN` | `X-User-Id` / `X-Domain` 头 |
| `WORKBUDDY_ENDPOINT` | 网关地址，默认 `https://copilot.tencent.com` |
| `TRAE_ACCESS_TOKEN` | Cloud-IDE-JWT token |
| `TRAE_DEVICE_ID` / `TRAE_EXTRA_HEADERS` | 设备头（缺头会被拒成 9074） |
| `QODER_PAT` / `QODER_GLOBAL_PAT` | 国内 / 国际版个人访问令牌 |
| `QODER_EXTRA_HEADERS` | Cosy 设备风控头 JSON |

用 `python -m checkin probe` 查看每个账号实际解析到的来源（脱敏）。

> 注意：`WORKBUDDY_ENDPOINT` 目前作为 `cred.endpoint` 用于 URL 构造；`QODER_*_HEADERS` 通过凭据提供器注入。若某平台需要新的附加字段，在凭据提供器 `credentials.py` 的 `_inject_common_env` 中登记即可。

## 多账号注意

- 每个账号的 `id` 必须唯一，否则配置校验失败。
- 同一平台的同一设备/版本每日可能只放行一个账号（如 Qoder 的 `SAME_PERSON_ALREADY_CLAIMED`）。这类限制应如实记录为 `already`/`drift`，不要靠伪造设备标识绕过。
- refreshToken 类凭据（如 Trae）是轮换链，不要在多个工具中同时刷新同一个账号。
