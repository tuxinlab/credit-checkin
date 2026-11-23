# 契约（Contract）格式说明

契约是适配层的"数据部分"：接口路径、鉴权方式、请求头/体、响应字段映射都写在 JSON 里。上游接口变化时**优先改这里**，无需改 Python。

文件名必须为 `<platform>.<version>.json`，例如 `workbuddy.cn.json`，否则注册表会拒绝加载。

## 顶层字段

| 字段 | 说明 |
|---|---|
| `platform` / `version` | `workbuddy|trae|qoder` / `cn|global` |
| `name` | 展示名 |
| `contract_version` | 自定义版本号，漂移记录会带上它，建议每次修改递增 |
| `status` | `template` / `active` / `experimental`，仅展示 |
| `template` | `true` 时永远走模拟模式，不会发真实请求（示例契约默认如此） |
| `note` | 说明 |
| `upstream_references` | 参考的社区仓库，便于日后追溯 |
| `defaults.timeout` | HTTP 超时秒数 |
| `defaults.headers` | 所有步骤共用的请求头 |
| `auth` | 鉴权方式，见下 |
| `steps` | 请求步骤数组，按顺序执行 |
| `response` | 响应字段映射与形状校验 |
| `change_signals` | 人工维护的"需要关注的变化"清单（文档用途） |

## auth

```json
"auth": {
  "type": "bearer",              // bearer | header | query | none
  "header": "Authorization",
  "prefix": "Bearer ",
  "query_param": "token"
}
```

- `bearer`：`Authorization: Bearer <token>`
- `header`：直接把 token 放到 `header` 指定的头
- `query`：拼到 URL 查询参数 `query_param=<token>`
- token 来源：优先凭据提供器（环境变量 / 本地登录态），即账号的 `credential_env` / `credential`
- `auth.headers`：自定义头模板，支持占位符，用于 `Cloud-IDE-JWT`、Cosy 设备指纹等平台专属鉴权

## steps

```json
{
  "id": "checkin",             // 唯一标识，用于日志
  "method": "POST",
  "url": "https://...",
  "headers": {"Content-Type": "application/json"},
  "body": {"source": "personal-checkin"},
  "when": {"var.campaign_id": {"exists": true, "truthy": true}},  // 条件步骤
  "capture": [                 // 从响应捕获动态值
    {"name": "campaign_id", "path": "$.campaigns",
     "select": {"actionType": "CLAIM_BENEFIT", "benefit.kind": "CREDITS", "claimStatus": "CLAIMABLE"},
     "field": "$.campaignId"}
  ]
}
```

占位符会在运行时替换：

| 占位符 | 含义 |
|---|---|
| `${token}` | 账号凭据（明文 token） |
| `${cred.xxx}` | 凭据附加字段，如 `${cred.uid}`、`${cred.device_id}`、`${cred.endpoint}` |
| `${var.xxx}` | 上一步 `capture` 捕获的值，如 `${var.campaign_id}` |
| `${account_id}` / `${date}` / `${platform}` / `${version}` | 基础信息 |

步骤按顺序执行，任一被判定为「已签 / 成功 / 失败」会立即返回；`when` 不满足的步骤会跳过。Qoder 用 `capture` 先取活动 ID 再带 `${var.campaign_id}` 领取。

> 请求头里的占位符如果解析结果为空，该请求头会被**直接丢弃**（不会发空值）。
> 这让"本机恰好没有某平台设备身份"时依然能发请求，例如 Qoder 的 `Cosy-MachineToken`：
> 缺失时平台会降级只返回订阅推广活动，因此 `credentials.qoder_device_identity()` 会尝试从
> `~/.qoder/umid-cache.json` 补齐。

## response

```json
"response": {
  "json_paths": {
    "success": ["$.code", "$.data.success"],
    "already": ["$.data.checked_in"],
    "failed":  ["$.code"],
    "credits": ["$.data.points", "$.credits"],
    "message": ["$.msg", "$.message"]
  },
  "values": {
    "success": [0, 200, true, "ok", "success"],
    "already": [true, 1, "already"],
    "failed":  [400, 401, 403, false, "failed"]
  },
  "expected_shape": {
    "type": "object",
    "required_paths": ["$.code", "$.data"],
    "field_types": {"$.data": "object"}
  }
}
```

- `json_paths` / `values`：v1 简单判定，作为兜底。
- `expected_shape`：结构契约。缺字段或类型不符 → 触发 `ContractDriftError`，状态记为 `drift`，**不再重试**。
- `verdicts`（v2，推荐）：**有序业务码判定规则**，优先级从上到下。每条：
  ```json
  {"status": "success|already|failed|skipped|drift",
   "steps": ["checkin"],              // 可选，限定生效步骤
   "message": "提示文案",
   "credits_path": "$.data.benefit.amount",
   "match": {"$.status": {"equals": ["CLAIMED", "SUCCESS"]},
             "$.msg": {"contains": "已签"},
             "var.campaign_id": {"truthy": true}}}
  ```
  匹配算子：`equals`（数组任一）/ `in` / `contains` / `exists` / `truthy`；多键为 AND。
- 若响应无法命中任何规则，**判定为漂移**，避免把未知响应误记为成功。

## 来源与可信度

每个契约的 `upstream_references` 标注了真实接口来源，`status` 标明可信度：
- `active`：社区项目实测接口（WorkBuddy CN / Trae CN / Qoder CN+Global）。
- `experimental`：需用户自己账号验证（Trae Global / WorkBuddy Global）。
- `template`：占位模板（本仓库已不使用）。

## 变更提醒机制

1. **检测**：每次执行做结构校验与业务码匹配，异常即写 `drift_events` 表。
2. **查看**：`python -m checkin drifts` 查看原因与响应样本（已脱敏）；`python -m checkin errors` 查看相关尝试日志。
3. **处理**：对照样本更新 `verdicts` / `capture` / `steps` 或端点，递增 `contract_version`，然后：
   ```bash
   python -m checkin validate
   python -m checkin run --simulate --force --platform <平台> --version <版本>
   ```
4. **通知**：配置 `notify.on_drift=true`（默认）与 webhook 后，漂移会主动推送。

## 接口变更后如何适配

1. 用 DevTools / 抓包观察客户端请求，记录 URL、头、体、响应 JSON。
2. 更新对应契约的 `steps`（端点/头/体）与 `response.verdicts`（业务码语义）。
3. 若是动态 ID（如活动 ID），在步骤里加 `capture`，后续步骤 URL 用 `${var.xxx}`。
4. 更新 `contract_version` 与 `change_signals`，运行 `validate` 与 `run --simulate` 验证。
