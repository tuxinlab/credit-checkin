# AGENTS.md — 项目开发笔记

> 给维护者与 AI 编码助手：这里记录"代码为什么长这样"和踩过的坑。
> 面向使用者的操作说明在 [README.md](README.md)，契约字段格式在 [contracts/README.md](contracts/README.md)，配置项含义在 [config/README.md](config/README.md)。

## 项目速览

- 入口 `python -m checkin <command>`；Web 面板 `python -m checkin web`，只监听 `127.0.0.1`。
- 核心链路：`config.py`（配置/账号）→ `plan.py`（当天任务+去重）→ `executor.py`（凭据/契约/重试/落库）→ `storage.py`（SQLite）。
- 契约是适配层的数据部分：`contracts/<platform>.<version>.json` 定义端点、请求头、`steps`、`capture`、`verdicts`。**接口变了先改契约，不要先改 Python。**
- 凭据优先级：环境变量 / 行内 → `data/vault.json`（DPAPI 加密）→ 本机客户端登录态（需显式允许）。任何日志与 API 返回都必须脱敏。

## 设计约定（改动时别破坏）

1. **按日幂等**：去重键 `platform:version:account_id:date`。`success` / `already` / `drift` 是当天终态；`skipped` 不是终态——缺凭据导致的跳过允许当天重跑。
2. **结果不回退**：`executor.finish_guarded()` 保证当天已有成功记录时，后续重跑判成 `skipped` / `failed` 不会覆盖原记录与积分；也不会留下僵死的 `running`。
3. **不做跨天补签**：平台活动按天结算，过期积分补不回来。`catchup_days` 只是兼容保留字段，`plan.py` 里已不生效。漏签保障靠主时间 + 可配置兜底时间 + `StartWhenAvailable` 开机补跑 + 失败重试。
4. **确定性失败不白等**：凭据失效（401 / `TOKEN_EXPIRE`）和契约漂移走 `fatal`，不进入退避重试。

## 已知陷阱（改代码前先看）

### Qoder：先看账号资格，再看设备风控

排查"没有可领取活动"时**先确认账号资格**：自 2026-11-19 10:00 起，每日 100 通用 Credits 仅面向 Pro 试用期用户及 Lite / Pro / Pro+ / Ultra 个人付费订阅用户开放，**免费体验版不再开放领取**（活动 2026-09-18 上线；奖励 30 天有效、未到期可跨天保留；调整前已领取的不受影响）。资格不符导致的"无活动"是平台规则，代码无需也无法绕过——**不要为此改契约或伪造标识**。确认账号符合资格后，才继续排查下面的设备风控问题。

### Qoder：设备风控会让每日活动"消失"

- **现象**：签到接口报告"没有可领取活动"，但网页端明明有 100 Credits 未领（账号已确认符合上述资格）。
- **原因**：活动列表被设备风控降级，只返回订阅推广活动（`VIEW_DETAILS`）；真正的每日活动 `CLAIM_BENEFIT` 根本不出现在 `campaigns` 里。
- **处理**：请求必须带一套 UMID 设备头。代码在 `credentials.qoder_device_identity()`：读本机 `~/.qoder/umid-cache.json` 的 `machineToken` / `machineCode` / `machineType` 与 `~/.qoder/.auth/machine_id`，拼成 `Cosy-MachineToken` / `Cosy-MachineCode` / `Cosy-MachineType` 等头（`contracts/qoder.*.json` 的 `defaults.headers`）；模板里还要有 `Cosy-ClientType: 10`。
- **覆盖方式**：`QODER_MACHINE_TOKEN` / `QODER_MACHINE_CODE` / `QODER_MACHINE_TYPE` / `QODER_MACHINE_ID` 环境变量优先。
- **注意**：请求头占位符解析为空时会被适配器直接丢弃（不发空值）。设备头缺失时平台会静默降级，不会报错——这正是"看起来像没活动"的根源，排查时先确认设备头是否齐全。
- 动态活动 ID 由 `steps[].capture` 从列表里筛选 `actionType=CLAIM_BENEFIT` 且 `claimStatus=CLAIMABLE` 的活动得到，自动排除 Pro 升级推广。

### HTTP 状态码必须参与判定

早期适配层只看响应 body 的业务码，忽略 HTTP 状态码——网关 500 或 401 若返回 `{"code":0}` 会被误判成签到成功、记 100 积分。现在 `adapters/http.py` 在 verdict / v1 兜底判成 success/already 时会检查 `last_status_code`：非 2xx 一律转 `failed`，401/403 标记 `auth_failure` 触发续期。改判定逻辑时别把这道防护去掉（`test_non_2xx_success_body_is_not_counted`、`test_http_401_marks_auth_failure` 守着）。

另外契约里**鉴权失败规则必须排在所有 success 规则之前**（`exists:true` 对 `null` 也成立，靠后的 success 会抢先命中）。WorkBuddy CN 契约曾犯过这个错，已修。

### 判定算子类型严格性

`rule_match` / `_when_met` 的 `in`、`equals` 走 `match_any`（严格区分 bool 与数字），`truthy` 必须排除 `_MISSING`/`None`——缺失哨兵是 `object()`，直接 `bool()` 会误命中。改动见 `base.py` / `http.py`，回归测试在 `test_adapters.py`。

### 更新检查：只提示不自动替换

`checkin/updater.py` 从 GitHub Releases API 读最新 tag 与本地 `__version__` 比对，Web 面板「契约状态」页显示更新提示和下载链接（`/api/version`）。**刻意不做自动下载替换**：开源个人工具自动执行下载文件风险高、易被杀毒误报。

- 仓库地址是 `updater.GITHUB_REPO` 常量，当前为 `tuxinlab/credit-checkin`；**fork 后必须改成自己的**，否则会跟随原仓库版本。未配置时接口静默返回 error、不影响签到。
- tag 格式必须是 `v` + 数字版本（`v0.1.1`），`_version_key` 只提取数字段比对。
- 结果缓存 1 小时，面板轮询不会反复请求 GitHub；`?force=1` 或点「检查更新」按钮强制刷新。
- 任何网络/API/解析失败都吞掉只记 error 字段，绝不抛异常到主流程。测试在 `tests/test_updater.py`。

### 打包成 exe：路径与计划任务的特殊处理

单文件 exe 用 PyInstaller 构建（`build.bat` → `main.py` → `checkin.__main__.entry()`）。有两处和源码模式不同，改动时务必留意：

1. **路径分两类**（`checkin/paths.py`）：只读资源（contracts/、web/）打包后在 `sys._MEIPASS`，源码在仓库根；可写数据（config.json、data/、logs/）exe 模式写 exe 同目录，不可写时降级 `%LOCALAPPDATA%/CreditCheckin`，`CHECKIN_HOME` 优先级最高。`data_root()` 会随 CHECKIN_HOME 变化自动失效缓存。**其它模块不要再用 `__file__.parents[1]`**，统一走 paths。
2. **计划任务指向 exe**（`manage.register_task()`）：`sys.frozen` 时注册 `exe路径 --run`，否则 `python -m checkin run`。exe 无参数=启动面板，`--run`=静默签到；`--noconsole` 下未捕获异常由 `_log_fatal()` 写进日志文件，否则用户看不到失败。

打包后契约内置在 exe 里，改契约需重新构建；面板「契约状态」显示的契约版本来自内置副本。构建脚本已带官方 PyPI 源兜底（默认源可能没有 PyInstaller）。回归测试在 `tests/test_paths.py`。

### 配置写操作必须加互斥锁

Web 面板是多线程，`config.json` 的每次"读→改→写"都必须是原子事务，否则并发添加账号会互相覆盖。`manage.py` 的写函数都带 `@locked_write`（`config_lock`：进程内 RLock + 进程间文件锁），`write_raw` 用唯一临时文件名。**新增写配置的函数时务必加 `@locked_write`**；新增裸 `read_raw→write_raw` 代码块时用 `with config_lock(path):` 包住。回归测试 `tests/test_concurrency.py`。

### `--restart` 只能结束确认身份的面板进程

`cli._kill_port_owner()` 绝不能按端口无差别 `taskkill`——会误杀恰好占用 8765 的其它服务。现在先按端口找 PID，再用 WMI 查命令行，只有含 `-m checkin` 且含 `web` 才结束；查不到身份就跳过。改这段时守住"宁可不杀，不可错杀"原则。

### 令牌续期：写回保险库后不能重新 resolve()

执行器鉴权失败触发续期时，`refresher.refresh()` 成功会把新 token 写回保险库。**不要**随后调用 `resolve_credential()` 重新解析——当账号凭据来自环境变量（`credential_env`）时，环境变量优先级高于保险库，重新解析会拿回旧的过期 token，续期等于白做、重试再次 401。

正确做法：`TokenRefresher.last_token` 记录了本次续期拿到的新 token，执行器直接用它覆盖内存里的 `cred_dict["token"]`（见 `executor.py` 的 `auth_failure` 分支）。`tests/test_executor.py::test_refresh_uses_new_token_even_when_credential_from_env` 是这条的回归测试，改动续期链路时务必保留。

### Qoder Global：401 不能被当成"没活动"

早期 `verdicts` 没覆盖 401，失败被记成 `skipped`，看起来像"今天没活动"。现在契约里显式加了 `fatal` / `auth` 规则：明确提示凭据失效，先尝试 `refresher.py` 的 deviceToken 续期再重试一次。排障时先看 `python -m checkin errors` 的原始响应，别只看当日状态。

### WorkBuddy / Trae 国际版：根本没有签到活动

Trae Global 没有每日签到入口；WorkBuddy Global 定价页写的「每日活跃奖励 30 积分/天」是按使用自动发放，无法手动领取。两者在 `models.UNSUPPORTED_VERSIONS` 中强制禁用，`config.load_config()` 与 `manage.set_account_enabled()` 都会拒绝启用——**不要为了"让界面能点"而删掉这层保护**。

### Web 面板：旧后端进程导致新按钮 404

浏览器连到上一次启动的旧进程时，前端新、后端旧，新 API 会 404。处理：后端提供 `GET /api/health` 带版本标识，前端启动时校验；`web --restart` 会结束占用端口的旧面板进程，`start.bat` / `启动面板.bat` 默认带 `--restart`。改了 API 时记得同步版本号。

### 模拟运行不能写真实数据库

`run --simulate` 与面板「模拟运行」必须走 `executor._memory_finish()`：`persist=False`，只返回内存结果、不写 SQLite，输出文案带【模拟】前缀。曾经模拟运行污染过真实签到记录，加测试守住这条。

### Windows 计划任务

- `AtLogOn` 触发器在普通权限下会被拒绝访问，改用 `Daily` 触发器；主时间 10:20 + 可选兜底时间（面板 `second_time`，默认 15:00，清空则单触发）。
- `InteractiveToken`（仅登录时运行）无法改成"不管用户是否登录都运行"而不保存密码，别在脚本里硬试；需要覆盖注销场景时用 `schedule` 守护模式。
- PowerShell 在中文 Windows 输出 GBK，`manage._decode_console()` 按 utf-8 → gbk → mbcs 依次解码。
- 设置里要有 `StartWhenAvailable` / `AllowStartIfOnBatteries` / `DontStopIfGoingOnBatteries` / `RestartCount=2`，笔记本缺了这些会频繁漏签。
- **验证电池选项不要用 `Get-ScheduledTask`**：这两个属性是 nullable，即使设置成功也显示为空，容易误判成"没写进去"。用 `Export-ScheduledTask -TaskName CreditCheckinDaily` 导出 XML，看 `<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>` 和 `<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>` 才是实际值。任务重新注册后 `LastTaskResult` 会短暂显示 `267011`（尚未运行），不是失败。

### 其它容易重犯的

- **弹窗遮罩**：`hidden` 属性与 CSS `display:flex` 冲突会导致弹窗关不掉；用 `style.display` 显式控制。
- **中文表格错位**：`print_table()` 按 East Asian width 计算列宽，不能直接 `len()`。
- **Windows 时区**：`zoneinfo` 可能缺 tzdata，配置校验放行 `utils.get_tz()` 内置的别名（`Asia/Shanghai` / `UTC+8` / `UTC` 等）。
- **测试隔离**：任何会写配置/保险库的测试都要设置 `CHECKIN_HOME` 指向临时目录（见 `tests/test_manage.py`），否则会污染真实 `data/vault.json`。
- **账号 ID**：由 `manage.next_account_id()` 自动生成（`平台缩写-版本-序号`，如 `qoder-gl-01`），只作为内部去重键，不要让用户手填。

## 改动后的验证清单

```bash
python -m checkin validate
python -m checkin run --simulate
python -m unittest discover -s tests -v
```

改契约时递增 `contract_version`，用 `python -m checkin drifts` 对照脱敏响应样本更新 `verdicts` / `capture` / `steps`。改 Web API 时检查 `/api/health` 版本与 `start.bat --restart` 路径。
