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

### 一键更新：替换 exe 的四个必踩坑

`updater.apply_update()` 下载新 exe 后生成 bat，由外部 cmd 在面板退出后完成替换（面板进程无法覆盖自己）。以下几点缺一不可：

1. **bat 必须 CRLF**。用 `write_bytes(content.replace("\n", "\r\n").encode("mbcs", "replace"))`；写成 LF 会让 cmd 把多行拼成一行，脚本静默失效且极难察觉。
2. **不要用 `start` 启动脚本**。无控制台环境下 `start "" /b` 会挂起，还会把空标题误当成文件（报「Windows 找不到 '\\' 文件」）。直接 `cmd /c <bat>` + `CREATE_NO_WINDOW` 即可，Popen 不等待，面板退出后脚本继续。
3. **启动新 exe 前必须清除 PyInstaller 环境变量**：`_PYI_APPLICATION_HOME_DIR`、`_MEIPASS2`、`_PYI_ARCHIVE_FILE`、`_PYI_PARENT_PROCESS_LEVEL`、`_PYI_SPLASH_IPC`。否则新进程继承旧的 `_MEIxxxx` 解压目录，而该目录已被上一进程清理，启动即崩（`No such file or directory: _MEIxxxx`）。
4. **用「能否 move 旧 exe」判断文件释放**，比查 PID 可靠：onefile 的 bootloader 可能在子进程退出后仍短暂持有句柄。

下载侧：只接受 `https://github.com/{GITHUB_REPO}/releases/download/` 前缀，限 150MB，校验 `MZ` 头；**校验失败删除临时文件时必须先关闭句柄**，否则 Windows 报占用。

### 数据目录固定（v0.1.2 起）

打包后 `data_root()` 固定为 `%LOCALAPPDATA%/CreditCheckin`，**不再跟随 exe 位置**——否则用户移动 exe 就会看到一套空数据。首次启动由 `migrate_legacy_data()` 把 exe 同目录的旧数据复制过来（只复制不删除）。源码模式仍用仓库根。测试涉及 frozen 行为时必须隔离 `LOCALAPPDATA`，否则污染真实用户目录。

### 更新检查：检查静默、替换必须用户确认

`checkin/updater.py` 从 GitHub Releases 的 **atom feed** 读最新 tag 与 `__version__` 比对，右上角徽章显示状态（`/api/version`）。有新版时点击可一键下载替换，但**绝不后台静默替换**——必须用户点确认，且失败能回滚。

- 优先用 `releases.atom`（`github.com` 域名，无 API 速率限制）；API 仅作回退补充说明。匿名 API 每小时仅 60 次/IP，共享出口极易 429。
- 仓库地址是 `updater.GITHUB_REPO` 常量，当前 `tuxinlab/credit-checkin`；**fork 后必须改成自己的**。未配置时接口静默返回 error、不影响签到。
- tag 格式必须是 `v` + 数字版本（`v0.1.2`），`_version_key` 只提取数字段比对。
- 结果缓存 1 小时；前端页面加载 + 每 30 分钟各检查一次，`?force=1` 强制刷新。
- 任何网络/解析失败都吞掉只记 error 字段，绝不抛异常到主流程。测试在 `tests/test_updater.py`。
- **注意**：atom feed 有 CDN 缓存，发版后几分钟内仍读到旧 tag，容易误判"更新检查坏了"。
- **发版必须上传两个附件**：`CreditCheckin.exe`（固定名，程序内一键更新按此名下载）+ `CreditCheckin-<版本>.exe`（同一文件另存，供用户手动下载时区分版本）。只传带版本号的会让老版本用户更新 404；只传固定名则用户从历史页面下载无法分辨版本。

### 打包成 exe：路径与计划任务的特殊处理

单文件 exe 用 PyInstaller 构建（`build.bat` → `main.py` → `checkin.__main__.entry()`）。有两处和源码模式不同，改动时务必留意：

1. **路径分两类**（`checkin/paths.py`）：只读资源（contracts/、web/）打包后在 `sys._MEIPASS`，源码在仓库根；可写数据（config.json、data/、logs/）**exe 模式固定写 `%LOCALAPPDATA%/CreditCheckin`**（与 exe 位置无关），源码模式写仓库根，`CHECKIN_HOME` 优先级最高。`data_root()` 会随 CHECKIN_HOME 变化自动失效缓存。**其它模块不要再用 `__file__.parents[1]`**，统一走 paths。
2. **计划任务指向 exe**（`manage.register_task()`）：`sys.frozen` 时注册 `exe路径 --run`，否则 `python -m checkin run`。exe 无参数=启动面板，`--run`=静默签到；`--noconsole` 下未捕获异常由 `_log_fatal()` 写进日志文件，否则用户看不到失败。

打包后契约内置在 exe 里，改契约需重新构建；面板「契约状态」显示的契约版本来自内置副本。构建脚本已带官方 PyPI 源兜底（默认源可能没有 PyInstaller）。回归测试在 `tests/test_paths.py`。

### 配置写操作必须加互斥锁

Web 面板是多线程，`config.json` 的每次"读→改→写"都必须是原子事务，否则并发添加账号会互相覆盖。`manage.py` 的写函数都带 `@locked_write`（`config_lock`：进程内 RLock + 进程间文件锁），`write_raw` 用唯一临时文件名。**新增写配置的函数时务必加 `@locked_write`**；新增裸 `read_raw→write_raw` 代码块时用 `with config_lock(path):` 包住。回归测试 `tests/test_concurrency.py`。

### `--restart` 只能结束确认身份的面板进程

`cli._kill_port_owner()` 绝不能按端口无差别 `taskkill`——会误杀恰好占用 8765 的其它服务。现在先按端口找 PID，再用 WMI 查命令行，只有含 `-m checkin` 且含 `web` 才结束；查不到身份就跳过。改这段时守住"宁可不杀，不可错杀"原则。

### 令牌续期：写回保险库后不能重新 resolve()

执行器鉴权失败触发续期时，`refresher.refresh()` 成功会把新 token 写回保险库。**不要**随后调用 `resolve_credential()` 重新解析——当账号凭据来自环境变量（`credential_env`）时，环境变量优先级高于保险库，重新解析会拿回旧的过期 token，续期等于白做、重试再次 401。

正确做法：`TokenRefresher.last_token` 记录了本次续期拿到的新 token，执行器直接用它覆盖内存里的 `cred_dict["token"]`（见 `executor.py` 的 `auth_failure` 分支）。`tests/test_executor.py::test_refresh_uses_new_token_even_when_credential_from_env` 是这条的回归测试，改动续期链路时务必保留。

### 凭据过期的分级恢复：本机优先，Qoder 最后走页面授权

鉴权失败时执行器调 `TokenRefresher.recover()`（不是 `refresh()`，后者只做协议续期、保留兼容）：
1. **本机客户端登录态优先**：用户本机一直登录着客户端，保险库里的 token 反而可能是旧的。Trae 用 `autofill_trae.auto_detect()`、Qoder 用 `autofill_qoder_local.auto_detect(version)`、WorkBuddy 用 `autofill.auto_detect()`。`recover()` 拿到新 token 和字段（device_id/uid 等）写回保险库，同时记到 `last_token` / `last_fields`，执行器直接用它们替换内存凭据（**同样不能重新 resolve()**）。
2. **refresh_token 协议续期兜底**：本机没取到才用 `refresh()` 的 deviceToken/ExchangeToken 续期。
3. **Qoder 仍失败 → 页面授权**：`recover()` 置 `need_login=True`，执行器把它作为布尔字段写进 `runs.detail`（**用布尔，不要用含 credential 字样的字符串键**，否则被 `sanitize()` 脱敏成 `<redacted>`，前端读不到）。`/api/run` 序列化出 `need_login`，前端 `runCheckin()` 检测到失败记录带该标记就调 `openQoderAuth()` 拉起设备授权弹窗，授权成功后自动 force 重跑该账号。计划任务 `--run` 无人值守，只能把这个信号留在记录里，用户下次打开面板时看到。

注意 `need_login` 信号只在 `attempt < max_attempts` 的恢复分支里产生；`max_attempts=1` 时不触发恢复（不阻塞快速失败语义）。回归测试：`test_local_recovery_then_retry_uses_new_token_and_fields`、`test_need_login_signal_when_all_recovery_fails`、`test_recover_prefers_local_over_refresh_token`、`test_recover_qoder_local_then_refresh_then_need_login`。

### Qoder Global：401 不能被当成"没活动"

早期 `verdicts` 没覆盖 401，失败被记成 `skipped`，看起来像"今天没活动"。现在契约里显式加了 `fatal` / `auth` 规则：明确提示凭据失效，先尝试 `refresher.py` 的 deviceToken 续期再重试一次。排障时先看 `python -m checkin errors` 的原始响应，别只看当日状态。

### WorkBuddy / Trae 国际版：根本没有签到活动

Trae Global 没有每日签到入口；WorkBuddy Global 定价页写的「每日活跃奖励 30 积分/天」是按使用自动发放，无法手动领取。两者在 `models.UNSUPPORTED_VERSIONS` 中强制禁用，`config.load_config()` 与 `manage.set_account_enabled()` 都会拒绝启用——**不要为了"让界面能点"而删掉这层保护**。

### Web 面板：旧后端进程导致新按钮 404

浏览器连到上一次启动的旧进程时，前端新、后端旧，新 API 会 404。处理：后端提供 `GET /api/health` 带版本标识，前端启动时校验；`web --restart` 会结束占用端口的旧面板进程，`start.bat` / `启动面板.bat` 默认带 `--restart`。改了 API 时记得同步版本号。

### 模拟运行不能写真实数据库

`run --simulate` 与面板「模拟运行」必须走 `executor._memory_finish()`：`persist=False`，只返回内存结果、不写 SQLite，输出文案带【模拟】前缀。曾经模拟运行污染过真实签到记录，加测试守住这条。

### Windows 计划任务

- **注册失败要区分「权限」与「其它」**。`Register-ScheduledTask` 报 `PermissionDenied` / `0x80070005` 是 Windows 当前账号无权创建计划任务（公司策略、安全软件、非管理员），不是契约或签到逻辑问题；`manage.register_task()` 已把这类错误翻译成可操作提示（管理员运行 / 手动创建任务）。不要把这类错误吞掉或改成「注册成功」。
- **定时任务必须和面板用同一套数据目录**。源码模式注册的是 `python -m checkin run`（写仓库根 `data/`），exe 模式注册的是 `exe --run`（写 `%LOCALAPPDATA%/CreditCheckin`）。若早期用源码注册过任务、后来改用 exe 面板，就会出现「平台实际领了、面板却显示没记录」——两个 SQLite 库各记各的。exe 面板启动时由 `manage.repair_scheduled_task()` 自检并把任务改回当前 exe（只改已存在且未禁用的任务）。排查此类问题先看任务实际命令（`Export-ScheduledTask`）和两份 `checkin.db` 的当日 runs，不要先怀疑签到接口。
- `AtLogOn` 触发器在普通权限下会被拒绝访问，改用 `Daily` 触发器；主时间 10:20 + 可选兜底时间（面板 `second_time`，默认 15:00，清空则单触发）。
- `InteractiveToken`（仅登录时运行）无法改成"不管用户是否登录都运行"而不保存密码，别在脚本里硬试；需要覆盖注销场景时用 `schedule` 守护模式。
- PowerShell 在中文 Windows 输出 GBK，`manage._decode_console()` 按 utf-8 → gbk → mbcs 依次解码。
- 设置里要有 `StartWhenAvailable` / `AllowStartIfOnBatteries` / `DontStopIfGoingOnBatteries` / `RestartCount=2`，笔记本缺了这些会频繁漏签。
- **验证电池选项不要用 `Get-ScheduledTask`**：这两个属性是 nullable，即使设置成功也显示为空，容易误判成"没写进去"。用 `Export-ScheduledTask -TaskName CreditCheckinDaily` 导出 XML，看 `<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>` 和 `<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>` 才是实际值。任务重新注册后 `LastTaskResult` 会短暂显示 `267011`（尚未运行），不是失败。

### 账号额度/到期查询（checkin/quota.py）

- 三个平台的额度接口都在签到契约之外，单独放在 `quota.py`，**只读 POST**，绝不碰签到/登录/续期接口：
  - WorkBuddy：`POST {endpoint}/billing/meter/get-user-resource-summary`（`endpoint` 默认 `copilot.tencent.com`），再从返回的 `Packages[].PackageCode` 决定查 `get-user-resource-free-packages` 还是 `paid-packages` 拿到期。**必须带契约同款头**：`Authorization: Bearer {token}` + `X-User-Id` + `X-Domain`；企业号再带 `X-Enterprise-Id`/`X-Tenant-Id` 并优先走 `get-enterprise-user-usage`。**`TotalCredits` 名字骗人，它其实是剩余积分**（等于 `CreditGroups[].CycleRemainCapacity` 之和）；总额必须汇总 `Packages[].CycleTotalCapacity`（或 `CreditGroups[].CycleTotalCapacity`），否则会显示成 `2038.28 / 2038.28` 这种错误比例。
  - Trae：`POST api.trae.cn/trae/api/v2/pay/web_user_ent_usage`，头 `Cloud-IDE-JWT {token}` + `x-device-id`。**实测真实响应没有 `code`/`remainingCredits` 这些字段**：剩余是 `total_balance`（或 `balance_summary[].total_balance`），总额是 `usage_summary.total_amount`，已用是 `usage_summary.consumed_amount`，到期在 `user_entitlement_pack_list[].entitlement_base_info.end_time`（**秒级**时间戳，不是毫秒）。只认旧字段名会误报「额度接口未返回可识别的积分字段」。
  - Qoder：`POST openapi.qoder.com.cn/api/v2/quota/usage`（国际版 `.sh`），剩余 = `userQuota.remaining + addOnQuota.remaining`，总额 = 两者 `total` 之和；`expiresAt=253402214400000` 是「永不过期」占位，不要显示。
- **不要再复刻 cc-switch 的 WBIPC 握手**。它借 WorkBuddy 主进程的登录身份查计费接口（HMAC 双向证明 + 命名管道），实现复杂且只在客户端运行时可用。实测本项目保险库里的 token 带上面几个头就能直接调通同样接口，数字与 cc-switch 完全一致（1938.28 / 体验版 / 788 分 12-17 到期）。
- **到期按「日期」合并**：同一天到期的多笔积分要相加（688.28 + 100 = 788），取当天最早一笔的时间；按精确时间戳分组会拆成两行，数量对不上 cc-switch。超远（10 年以上）视为永不过期；`CapacityType==4` 且按天切片的资源跳过。
- 缓存 5 分钟（`QuotaCache`，进程内），页面「刷新额度」传 `force=1` 跳过；单账号失败降级为 `ok=false` + 明确 `reason`，不影响其它账号。API `/api/quota`，新增能力标记 `quota.overview` 并加入前端 `REQUIRED_CAPS`，避免旧后端 404 时页面无提示。
- 额度展示放在「今日概览 → 今日各账号状态」卡片内部（和签到记录同一张表区域），**不要**单独占「账号配置」页的卡片：账号配置页已经有账号表格 + 自动签到 + 登录态探测，再来一张额度卡太挤。前端 `loadOverview()` 负责拉额度并绑定刷新按钮，`LOADERS.accounts` 不再调用 `loadQuota()`。
- token 是否过期以实际返回为准：同一账号不同时间可能 401，也可能是接口结构没对上而被误判为失败，先看原始响应再下结论。测试用 mock，不依赖真实网络与真实 token。

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
