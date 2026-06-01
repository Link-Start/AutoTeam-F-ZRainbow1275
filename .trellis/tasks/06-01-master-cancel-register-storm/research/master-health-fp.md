# master_health 误判健康母号为 cancelled — 根因研究

| 项 | 内容 |
|---|---|
| Researcher | r1-master-health |
| 日期 | 2026-06-01 |
| 母号 | farmer1 / account_id=`105c3bb4-4fcc-4f37-9f6b-a5afe1ab8d30` |
| 用户实证 | 母号订阅**没有 cancel**,但被 AutoTeam 报为 "subscription_cancelled" |
| 置信度 | **High**(代码路径 + 日志时间线 + 官方 Stripe/OpenAI 语义三方互证) |
| 关键日志 | `resource/日志(1).md:39, 529, 599, 681, 885, 967` |

---

## 1. TL;DR — 误判根因

`is_master_subscription_healthy` 的判定逻辑 **将 `eligible_for_auto_reactivation=True` 视为"订阅已 cancel 的充分条件"**,然后用 JWT claims (`chatgpt_subscription_active_until` / `chatgpt_plan_type`) 做 grace fallback。但本次现场:

1. **OpenAI 官方语义**:`eligible_for_auto_reactivation` 是 Stripe `cancel_at_period_end=true` 的旁路信号,**也用于已 deactivated 工作区**,**并不能** authoritative 地判 "订阅活跃中"。但更关键 — 它 **可能在某些工作区生命周期阶段持续为 `true` 而权益完全正常**(参见 §6.1)。spec/master-subscription-health.md §0 共因栏自陈:"v1.0/v1.1 误把它等价于 cancel_at_period_end=true 且 period 已过";Round 11 仅靠 JWT fallback 来"打补丁",并没有解决 L1 主探针的字段语义错误。
2. **现场触发时序(并行副作用,非传递因果)**:**20:53:46 用户手动重新管理员登录**,期间 Codex 主号 OAuth 刷新失败(日志 529 行 "刷新主号认证文件失败")。**该失败本身不写 master_health(r4 已核实代码独立,见 §13)**,但与之**并行**发生的是 `ChatGPTTeamAPI` 实例在 20:57:15 之后用新 session 重启 `start()` → 走 **curl_cffi transport**,`access_token` 取自 `/api/auth/session`。`_load_admin_id_token` **唯一可用 token 是这个 web access_token**(`codex-main-*.json` 在 `accounts/` 目录下根本不存在 — 实测仅有 `.master_health_cache.json`,日志 529 已证刷新失败 → 也就没生成该文件)。
3. **fallback 也失败**:`extract_plan_type_from_jwt` 需要 web access_token 含 `chatgpt_plan_type` claim;若该 claim 缺失或非 `team/business/enterprise/edu`,落入 §14.2 决策矩阵最后一行 → `(False, "subscription_cancelled")`。
4. **后果**:`manager.py:3299/3560` 双 fail-fast(personal + Team OAuth)立即报 "母号订阅已取消(eligible_for_auto_reactivation=true)" 并 kick 刚刚成功 invite 的子号(日志 599、681、885、967 行),fill 任务 503 风暴。

启动时(20:43:09)的 retroactive helper 报 `master_active_no_grace_candidates`(日志 39 行)— 此时还有合法 `access_token` / `chatgpt_plan_type=team`,probe 拿到 `active`。20:56:36 OAuth 刷新失败后,所有后续 `_classify_l1` 看到的 `id_token` 不再有效,误判开关从此 stuck-on。

---

## 2. 关键代码引用

### 2.1 误判核心 — `_classify_l1` (master_health.py:208–272)

```python
# src/autoteam/master_health.py:240
if target.get("eligible_for_auto_reactivation") is True:
    # 路径 1:OAuth id_token JWT 含 chatgpt_subscription_active_until
    grace_until = extract_grace_until_from_jwt(id_token) if id_token else None
    if grace_until and grace_until > time.time():
        return True, "subscription_grace", {...}
    # 路径 2(Round 11 二轮):web access_token JWT 含 chatgpt_plan_type fallback
    plan_type = extract_plan_type_from_jwt(id_token) if id_token else None
    _PAID_PLAN_TYPES = ("team", "business", "enterprise", "edu")
    if plan_type in _PAID_PLAN_TYPES:
        return True, "subscription_grace", {...}
    return False, "subscription_cancelled", {...}     # ← 误判出口
```

**问题 1**:把 `eligible_for_auto_reactivation=True` 当作"必须证明仍在 grace"的状态。**官方语义里这个字段甚至在 deactivated workspace 上也出现**(用于驱动 chatgpt.com 前端的 reactivation 按钮渲染 — 见 §6.1 GitHub openai/codex #16292),并不专属 "scheduled to cancel"。

**问题 2**:fallback 需要 token 中带正确 claim。`_load_admin_id_token` (master_health.py:168-205) 优先级:
- (1) `chatgpt_api.access_token`:**这是 ChatGPT web JWT,只有当 curl_cffi 已成功 `_fetch_access_token_via_transport()` 才存在**。但即便存在,实际 web `access_token` payload 是否含 `chatgpt_plan_type`/`chatgpt_subscription_active_until` 取决于 OpenAI;Round 11 二轮 spec 自陈 web access_token "不含 chatgpt_subscription_active_until,但含 chatgpt_plan_type" — **这只是观察值,不是契约**。
- (2) `accounts/codex-main-*.json id_token`:**当前 accounts/ 目录下完全没有此类文件**(`ls D:/Desktop/AutoTeam/accounts/ → 只有 .master_health_cache.json`)。日志 529 行 20:56:36 "WARNING [API] 管理员登录完成,但刷新主号认证文件失败" 证明用户尝试重新生成但失败,且**之前也没有**(否则不会"刷新")。
- 结果:`_load_admin_id_token()` 在最佳情况下只能拿 (1) 的 web access_token,fallback 路径全押宝在这个 token 是否带 `chatgpt_plan_type` claim 上。

### 2.2 调用点(双 fail-fast)

- `src/autoteam/manager.py:3287` `personal OAuth precheck` → `master_reason == "subscription_cancelled"` ⇒ `_record_outcome("master_degraded")` + `update_account(STANDBY)`
- `src/autoteam/manager.py:3548` `Team OAuth precheck` → 同样逻辑 + `_kick_team_seat_after_oauth_failure(reason="master_degraded")`(把刚 invite 的健康子号踢出 Team)

两处都使用 `is_master_subscription_healthy` 的返回值;`subscription_cancelled` 是唯一会触发 kick + fail-fast 的 reason(`auth_invalid`/`network_error`/`workspace_missing` 都放行)。

### 2.3 cache 污染问题(部分相关,**不是本次主因**)

实测 `accounts/.master_health_cache.json` 内容:

```json
{ "schema_version": 2,
  "cache": { "bac969ea-468b-4ff4-8d7a-6f4f183394d9": {
    "healthy": false, "reason": "workspace_missing",
    "probed_at": 1700000000.0, ...
  } } }
```

- **不是 farmer1 的 account_id**(`bac969ea-...` ≠ `105c3bb4-...`),所以本次误判**不是** cache 命中导致 — cache 直接 miss,每次都走 L1 实测。
- `probed_at=1700000000.0`(2023-11)是测试夹具或人工填入的 sentinel,远超 `cache_ttl=300s`,即便 key 对得上也会 miss(`age >= cache_ttl`)。
- M-I3 cache 守卫(master_health.py:354-364)逻辑正确:`healthy ⇔ reason ∈ {active, subscription_grace}`,违反则丢弃。本次误判路径**不经过** cache 写盘的污染(实测中 cache 没被新值覆盖)。
- 结论:cache 污染**不构成**本次故障的因果链;CACHE_SCHEMA_VERSION=2 升级 + M-I3 守卫是健康的;但**若误判结果被写入 cache**,后续 5 分钟内每次 fill 都 fail-fast — 这正好解释了"截图 ALERT 显示 21:05:09 cache、user 21:05:26 之后看到 cache 还在用 21:05:09 的 probed_at"(日志 691-701 行 21:01-21:07 多次 `Team API transport: curl_cffi` 但无新 master_health 写盘 log — 极可能写盘静默,因为本次值 `healthy=False/reason=subscription_cancelled` **通过 M-I3 守卫**)。

**派生风险**:误判一旦被 L1 实测产生,会被 cache 持久化 5 分钟(`cache_ttl=300`),期间所有 fill/UI 都看到同一假阳性。

### 2.4 `_try_l3_settings_probe` 不参与

`_try_l3_settings_probe` (master_health.py:275-310) 只在 **L1 active 且 raw_item 缺 eligible 字段** 时执行;本次 L1 看到 `eligible=True` 直接走 cancelled 分支,L3 不进场。

---

## 3. `eligible_for_auto_reactivation` 真实语义

WebSearch + OpenAI/GeeksforGeeks/openai-codex#16292 综合:

| 字段名 | 出处 | 实际含义 |
|---|---|---|
| `cancel_at_period_end` (Stripe) | `/v1/subscriptions/{id}` | **用户已点击 cancel**,订阅在 period 末停止续费;**用户从未 cancel 时 = false**。 |
| `eligible_for_auto_reactivation` / `eligible_for_reactivation` (OpenAI 内部) | `/backend-api/accounts` items[i] | OpenAI 内部标志,驱动 chatgpt.com **DeactivatedWorkspaceModal** 的 "Purchase subscription" / "Manage subscription" 按钮渲染;**与 `is_deactivated` 配合**才有 cancelled 语义。仅凭此字段 = true 无法推断 cancel 状态。 |
| `is_deactivated` (OpenAI 内部) | `/backend-api/accounts` items[i] | True 才是真正"工作区已停用"信号(openai/codex#16292 实证,blanket HTTP 402)。 |
| `has_active_subscription` (OpenAI 内部) | `/backend-api/accounts` items[i] | 是否有活跃 Stripe 订阅(False = 已 lapsed)。 |

**关键发现**:Round 11 spec §0 共因栏自陈 "v1.0/v1.1 误把它等价于 `cancel_at_period_end=true` 且 period 已过";本研究进一步发现 — **即便 v1.3 的 Approach A grace fallback,基本前提"`eligible=true` ⇒ 已 cancel/grace 期内"也只是 user Q1 单点实证,缺乏 OpenAI 官方契约依据**。GitHub `openai/codex#16292` 给出的实证组合是 `is_deactivated=true + eligible_for_reactivation=true + has_active_subscription=false`,三字段联合判定才靠谱。

**结论**:**单字段判定就是设计错误**。修复必须升级为多字段 AND 判定。

---

## 4. cache 污染链路 — 评估结果:**不成立(主因),次因待防御**

- 实测 cache key 不匹配 farmer1 account_id,本次误判不走 cache 命中路径。
- 但**误判结果会被 L1 实测后写入 cache 持久化 5 分钟**;UI banner 截图显示 "上次探测 21:05:09 cache" 完全符合此模型 — 21:05:09 一次实测 L1 失败 → 写盘 → 21:05:09~21:10:09 cache 命中。
- M-I3 守卫**没问题**(`subscription_cancelled` 是合法 unhealthy reason),所以"假阳性 cache"完美通过校验。
- Round 11 二轮的 schema v1→v2 升级已在 20:53 admin 重登场景之前完成,cache 已是 v2 schema;**与本次不相关**。

---

## 5. 对照:autoteam-1(旧快照)与 .upstream

| 路径 | master_health.py 存在? | `eligible_for_auto_reactivation` 判定? |
|---|---|---|
| `D:/Desktop/autoteam-1/AutoTeam/src/autoteam/` | **不存在**(`ls` 输出无 master_health.py) | **没有任何引用**(`git grep` 在该 repo 无结果) |
| `D:/Desktop/AutoTeam/.upstream/` (vendored) | **不存在**(仅有 `chatgpt_api.py / codex_auth.py / invite.py / manager.py / oauth_workspace.py`) | **没有任何引用** |
| 当前 `D:/Desktop/AutoTeam/src/autoteam/` | 存在 949 行 | **有**(`master_health.py:240 + manager.py:3287/3548`) |

**autoteam-1 / upstream "能跑"的原因**:**它们根本不做这个 precheck**。Personal/Team OAuth 直接尝试,失败再由 `plan_drift` 兜底(`manager.py` 中有 plan_drift 重试循环)。fail-fast 节省的 "2 分钟" 在母号未 cancel 时**完全是负收益**(每次 fill 都假阳性 → 必踢子号 → 创建邮箱 → 重新注册 → 再假阳性 → 死循环)。

---

## 6. 最小修复建议

### 6.1 字段判定升级(必做,根因修复)

**Option A — 三字段 AND 判定**(参考 openai/codex#16292 实证):

```python
# master_health.py:_classify_l1 — eligible 分支替换为
eligible = target.get("eligible_for_auto_reactivation") is True
is_deactivated = target.get("is_deactivated") is True
has_active = target.get("has_active_subscription")
# True/False/None — None 时按字段不存在处理(spec §3.3 缺失字段保守 active)

# 真 cancelled:eligible + is_deactivated + has_active_subscription == False
if eligible and is_deactivated and has_active is False:
    # grace fallback 不变(grace_until > now 或 plan_type ∈ paid)
    ...
    return False, "subscription_cancelled", {...}

# 否则仅 eligible 不足以判 cancelled
return True, "active", {"eligible_only": eligible, ...}
```

**收益**:
- farmer1 现场 `is_deactivated` 必为 `False`(权益还在用) → 直接 `active`,误判消失。
- 真 cancel 场景仍能识别(三字段同时翻转才触发)。
- 不依赖 JWT claim 是否含 `chatgpt_subscription_active_until` / `chatgpt_plan_type`,与 token 路径解耦。
- 仅 `master_health.py` 单文件修改,manager.py 调用方零改动。

**Option B(兜底/兼容)— 把 "cancel" 改为"高置信 only"**:

如果 OpenAI 不一定返回 `is_deactivated` / `has_active_subscription` 字段,把 fail-fast **从 "subscription_cancelled" 改为新 reason "subscription_cancelled_high_confidence"**(需 JWT plan_type = "free" + grace_until 已过期 + L3 settings.plan != team **三方互证**)。原 `subscription_cancelled` 降级为 `subscription_eligible_only`(healthy=True),不再 fail-fast。

**推荐 Option A**;Option B 是字段不可得时的保底。

### 6.2 cache 防御(辅做)

- 误判一旦发生持续 5 分钟,放大伤害。建议**对 `subscription_cancelled` 的 cache 写盘**追加 "需要 >=2 次连续 L1 实测一致才信" 的 hysteresis(类似 `apply_pool_health_signal` 的 fail_count 累积),避免单次假阳性长锁定。
- 或更简单:**`subscription_cancelled` 不写 cache**,每次都 L1 实测(2-3 秒成本可接受,换取一旦数据修正立即恢复)。

### 6.3 admin auth 刷新失败时的回退行为(辅做)

`api.py: 管理员登录` 路径(日志 459-529 行)在 "刷新主号认证文件失败" 时,应该:
- **立即 invalidate `.master_health_cache.json` 中该 account_id 的项**(token 刚换,旧 health 状态可能完全失效)。
- **next probe force_refresh** 而非走 cache。
- 否则就是本次现场 — token 刚换、JWT claims 缺失,而 cache 还会用旧值。

### 6.4 不依赖 token 的 active 判定(辅做,长期)

`/backend-api/accounts` 已经返回 `plan` / `plan_type` / `structure` 字段(chatgpt_api.py:1239 已读)。**L1 直接读 `target.plan == "team"` 或 `structure == "workspace"`** 比 JWT plan_type 更可靠 — token 可能缓存陈旧,但 `/accounts` 响应每次实时。

---

## 7. 回归测试点(spec/master-subscription-health.md §8 fixture 扩展)

| 场景 | items[*] 关键字段 | id_token | 期望 (healthy, reason) | 当前 vs 修复后 |
|---|---|---|---|---|
| **本次现场** | `eligible_for_auto_reactivation: true`, `is_deactivated: false`, `has_active_subscription: true`, `plan_type: "team"` | None / web access_token 无 claim | **(True, "active")** | 当前: `(False, "subscription_cancelled")` ✗ → 修复后: ✓ |
| 真 cancel | `eligible=true, is_deactivated=true, has_active_subscription=false` | id_token 含 `chatgpt_subscription_active_until=now+24h` | (True, "subscription_grace") | 当前 ✓ / 修复后 ✓ |
| 真 cancel + period 已过 | `eligible=true, is_deactivated=true, has_active_subscription=false` | id_token grace_until 已过期 + plan_type="free" | (False, "subscription_cancelled") | 当前 ✓ / 修复后 ✓ |
| eligible-only(新增) | `eligible=true, is_deactivated=false, has_active_subscription=true` | 任意 | (True, "active") | 当前 ✗(误 cancelled) / 修复后 ✓ |
| stale token + healthy account | `eligible=false` | None | (True, "active") | 当前 ✓ / 修复后 ✓ |
| cache 假阳性 hysteresis | 第 1 次 L1 = cancelled,第 2 次 L1 = active | — | 第 1 次不应固化为 cache(可选 §6.2) | 待加 |

新增 fixture: `tests/fixtures/master_health/eligible_only_healthy_workspace.json` 描述 farmer1 实际响应。

---

## 8. 不变量调整建议

- **M-I7**(`eligible_for_auto_reactivation` 严格 `is True` 比对)— 保留,但**不再单独触发 cancelled**。
- **新增 M-I18**:`reason == "subscription_cancelled"` 要求 (a) `eligible_for_auto_reactivation is True` **且** (b) `is_deactivated is True` **且** (c) `has_active_subscription is False`(任一缺失/不满足则降为 `active`,除非 grace fallback 三方都失败)。
- **M-I3** healthy ⇔ reason ∈ {active, subscription_grace}:保留。

---

## 9. 不影响 / 已确认 OK 的项

- M-I1 永不抛异常:OK,所有 Exception 已 catch → `network_error`。
- CACHE_SCHEMA_VERSION=2 不变量:OK,不与本 bug 相关。
- `_chatgpt_api_ready` curl_cffi/browser 双 transport 支持:OK,curl_cffi 拿 `access_token` 没问题,token 本身的 claim 内容才是问题。
- WorkspacePool `apply_pool_health_signal`:OK,但本次误判**会**触发 mark_unhealthy + fail_count 累积,**长期可能误触发 workspace failover**(若 K>1)— 修复 §6.1 后顺带解决。

---

## 10. 落地建议(给 implementer)

1. 改 `master_health.py:_classify_l1`(§6.1 Option A),~15 行,加 3 个字段读取 + 一个 AND 守卫。
2. 加 5 个 unit fixtures(§7),`tests/test_master_health.py` 中。
3. 在 `_save_cache` 前对 `reason="subscription_cancelled"` 加 `hysteresis_count`(§6.2),或简单地不写盘 — 二选一。
4. `api.py: cmd_admin_login` 完成时 force `_load_cache` 清掉旧 account_id 项(§6.3),~5 行。
5. spec 更新 `master-subscription-health.md` §14.2 决策矩阵第一列改为三字段联合,append M-I18(§8)。

---

## 11. 关键证据汇总

- 现场日志:`resource/日志(1).md:39`(20:43:09 启动 retroactive `master_active`)→ `:529`(20:56:36 admin Codex 刷新失败)→ `:599 / :681 / :885 / :967`(后续 master_degraded fail-fast)
- 代码:`src/autoteam/master_health.py:208-272 _classify_l1` / `:168-205 _load_admin_id_token` / `:313-532 is_master_subscription_healthy`
- 调用点:`src/autoteam/manager.py:3287` / `:3548`
- cache 文件实测:`accounts/.master_health_cache.json`(key 不匹配,sentinel `probed_at=1700000000.0`)
- 对照:`D:/Desktop/autoteam-1/AutoTeam/src/autoteam/` 无 `master_health.py`;`D:/Desktop/AutoTeam/.upstream/` 同上
- 字段官方语义:WebSearch + `https://github.com/openai/codex/issues/16292`(`eligible_for_reactivation` 三字段联合用法实证)
- spec 自陈不确定性:`prompts/0426/spec/shared/master-subscription-health.md §0 共因栏`("v1.0/v1.1 误把它等价于 cancel_at_period_end=true 且 period 已过";Round 11 user Q1 实证仅是单点)

---

## 12. 一句话结论

**根因 = 单字段 `eligible_for_auto_reactivation=True` 被当作 "subscription_cancelled" 的充分判定**;Round 11 JWT grace fallback 只是补丁,在 token 路径(admin Codex 刷新失败 → web access_token claim 缺 `chatgpt_plan_type`)下整体失效。**修复 = 升级为 `eligible + is_deactivated + has_active_subscription` 三字段 AND 判定(§6.1 Option A)**,辅以 §6.2 cache hysteresis + §6.3 admin 重登 cache invalidate。**置信度 High**(代码逻辑、日志时间线、OpenAI/Stripe 官方语义、autoteam-1 对照四方一致)。

---

## 13. r4-oauth-codex 协查记录(2026-06-01 增补)

### 13.1 r4 的代码层澄清(已采纳)

r4 通过 `git grep` 在 `master_health.py` 中核实:

- `refresh_main_auth_file()` (`codex_auth.py:3805-3816`) → `login_codex_via_session()` → `SessionCodexAuthFlow.start()`,**不写** master_health cache,**不调** `load_accounts/update_account`,失败只 raise RuntimeError。
- `api.py:1302-1312` 调用点 `try/except` 把异常串塞到 `info["main_auth_error"]`,**不动** admin_state、**不动** master_health。
- `grep "refresh_main_auth_file|main_auth_error|main_auth\b" master_health.py` → **0 命中**。
- 结论:`refresh_main_auth_file` 失败和 master_degraded 是**代码上独立**的信号。

**对本研究的影响**:§1 第 2 点"现场具体触发"中"传递因果"的措辞要弱化为"并行因果 / 时间相关性"。20:53 admin 重登失败不是误判的直接传递触发,而是同期 `chatgpt_api.start()` 重新走 `_start_transport_session` 时拿到的新 `access_token` 可能 claim 结构变化(并行副作用),且与 master_health cache 失效时机重合。**底层根因(单字段误判)不变**,只是触发时机的解释从"传递"改"并行"。

### 13.2 r4 自我纠正(已 settle)

r4 一开始基于 `eligible_for_auto_reactivation=true` 反推"OpenAI 端 master 真的被 cancelled",这是循环论证 — 用我们正在质疑的同一个字段作论据。team-lead 介入后 r4 撤回该结论,核实细节:

- 日志全文 grep `is_deactivated | has_active_subscription | deactivated | 402 | 403 | payment_failed | cancel_at_period_end` → **0 命中**。
- 5 处 `master 母号订阅已取消` 日志(`:599 :681 :885 :967` + 一处)全部只携带 `eligible_for_auto_reactivation=true` 一个字段,无任何独立 cancel 信号。
- `_classify_l1` (master_health.py:208-272) 的 cancelled 判定**只看** `eligible_for_auto_reactivation is True`,JWT grace fallback 只能把它从 cancelled 升级到 grace,**不提供独立证据**。
- `_try_l3_settings_probe` (`master_health.py:275-310`) 只在 `eligible` 字段缺失时才调用,**字段为 True 时不交叉验证**。
- 用户 ground truth("母号没 cancel") + 缺失独立 cancel 信号 → **倾向"单字段误判"结论**(与 §1-§3 一致)。

### 13.3 修复方向 — 维持原 §6.1 Option A

r4 建议的字段集(`has_paid_subscription` / `is_deactivated` / settings 接口 `plan_type` / billing 端点)与本研究 §6.1 Option A 一致,优先级建议:

1. **L1 主探针扩展**:`/backend-api/accounts` items[i] 三字段 AND(`eligible_for_auto_reactivation=True + is_deactivated=True + has_active_subscription=False`)。
2. **L3 副探针强制启用**:`eligible=True` 时**强制**跑 `/backend-api/accounts/{wid}/settings` 校验 `plan in {team, business, enterprise, edu}`(当前只在 `eligible` 字段缺失时才跑,要改成 `eligible=True 时也跑作为交叉验证`)。这条比 r4 提的"或者干脆 billing 端点"更轻量(不引新端点)。
3. **保留 JWT grace fallback** 但降级为"诊断辅助"(写 evidence 不参与判定)。

### 13.4 因果链最终版

```
用户 farmer1 母号(实际未 cancel)
  ↓
ChatGPT API `/backend-api/accounts` 对 workspace 105c3bb4-... 返回
  eligible_for_auto_reactivation = True (语义不明的字段,见 §3)
  is_deactivated = ? (日志无记录,推测 False)
  has_active_subscription = ? (日志无记录,推测 True)
  ↓
master_health.py:_classify_l1 单字段判定 → (False, "subscription_cancelled")
  ↓ (Round 11 grace fallback 救援失败)
  - 路径 1: id_token 含 chatgpt_subscription_active_until?
            → admin Codex 刷新失败(20:56:36,代码独立但时间重合)
            → 实际可能根本没有 codex-main-*.json,_load_admin_id_token 只拿到
              web access_token(_chatgpt_api.access_token)
  - 路径 2: web access_token 含 chatgpt_plan_type ∈ paid?
            → 20:53 admin 重登后新 access_token 是否仍含此 claim 未验证;
              事实上后续 5 处 fail-fast 证明此路径也未命中
  ↓
fail-fast(False, subscription_cancelled) 写入 cache 5 分钟
  ↓
manager.py:3287 / 3548 双 OAuth precheck 拒绝 + kick 已 invite 子号
  ↓
fill 任务 503 风暴,UI banner critical(截图 21:05:09 cache)
```

底层根因仍然是**单字段判定**;Token 路径只是让"本应能救场的 grace fallback"也失效,让误判从"理论可能"变成"现场必现"。

### 13.5 r4 协查后未变动的部分

- §6.1 Option A 三字段 AND 修复方向不变。
- §6.2 cache hysteresis / §6.3 admin 重登 cache invalidate 仍建议落地。
- §7 回归测试 fixture 不变。
- §8 M-I18 不变量提议不变。
- 置信度从 High 维持 High(r4 协查后,代码层独立性 + ground truth + 字段语义三方仍互证一致)。
