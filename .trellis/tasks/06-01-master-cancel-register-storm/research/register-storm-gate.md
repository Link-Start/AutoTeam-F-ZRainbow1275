# Register Storm — Master Health Gate Placement Research

Author: r3-register-gate · Round 12 · 2026-06-01
Subject: 母号订阅 cancel 时,免费号批次 / 巡检自动加号 / 手动加号 三入口在"建临时邮箱 + 开浏览器注册"之前完全没有 master_health 短路,直到注册完成走到 `_run_post_register_oauth_*_precheck`(manager.py:3299/3560) 才 fail-fast,造成每轮 CloudMail + Playwright + OpenAI 速率配额全浪费。

---

## 1. 三个注册入口的代码位 (file:line + 函数链)

### 1.1 免费号批次 (leave_workspace=True path)
HTTP / CLI:
- `POST /api/tasks/fill {target, leave_workspace:true}` → **api.py:3445** `post_fill()`.
- 入口已有一个 503 precheck(api.py:3475-3517,详见 §2.1),命中后整个任务不启动。这条路径在 leave_workspace=True 时也有覆盖。

Task 主体(被 _start_task 调度):
- `cmd_fill(target, leave_workspace=True)` → **manager.py:7090** dispatch
  → `_cmd_fill_personal(target)` → **manager.py:7353**
  → 第 7504 行 `create_new_account(None, mail_client, leave_workspace=True, out_outcome=outcome)`
  → **manager.py:5593** `create_new_account()`
  → **manager.py:5658 / 5699** `create_account_direct(...)`
  → **manager.py:5415** `create_account_direct()`
  → **manager.py:5121** `_attempt_chatgpt_signup_only()` —— 这里 5126 行立刻 `mail_client.create_temp_email()`,5206/5217 行进 `_register_direct_once`(全浏览器流程)
  → **manager.py:5495** `_run_post_register_oauth(...)` 才到现有 3299 行 personal 分支 fail-fast 闸。

### 1.2 巡检自动加号 (auto-check loop)
- 后台线程 `_auto_check_loop` → **api.py:3993**, 间隔默认 5 分钟。
- 两条注册触发路径:
  a) **auto-fill (cmd_rotate)** —— api.py:4231-4278 当 `active < sub_account_target` 命中冷却外侧时:
     `_start_task("auto-fill", cmd_rotate, {target_seats}, ...)` → **manager.py:6339** `cmd_rotate`
     → 6761 行 `create_new_account(chatgpt, ensure_mail())`(只在 standby 复用补不齐时才到这里)。
  b) **auto-replace (cmd_replace_batch)** —— api.py:4300-4344 当存在 low_accounts 即时替换:
     `_start_task("auto-replace", cmd_replace_batch, {emails, trigger:"auto-check"}, ...)` → **manager.py:6153**
     → 6171 行 `_replace_single(...)` → **manager.py:5984**
     → 6117 行 `create_new_account(chatgpt, mail_client)`(无 standby 可复用即建新号)。
  c) **provider-auth 低水位预防性 rotate** —— api.py:4366-4385 也会 `_start_task("auto-rotate", cmd_rotate, ...)`,同 (a) 的下游。
- 此外 api.py:4080 触发 `cmd_cleanup`,不进注册路径,这里不计。
- **关键事实**:这三条 auto-check 触发路径都从 `_auto_check_loop` 内直接 `_start_task(...)`,跳过 `post_fill`/`post_add` 的 HTTP handler,**任何 master_health probe 都不在巡检触发的链路上**。

### 1.3 手动 / API 加号
- `POST /api/tasks/add` → **api.py:3436** `post_add()` —— **完全没有任何 master_health 预检**, 直接 `_start_task("add", cmd_add, {})`。
- `POST /api/tasks/replace` → **api.py:3415** `post_replace()` —— 同样无预检,直接 `_start_task("replace", cmd_replace_one, ...)`。
- `cmd_add()` → **manager.py:6834**, 6842 行 `create_new_account(chatgpt, mail_client)`。
- `cmd_replace_one(email)` → **manager.py:6132**, 6143 行 `_replace_single(...)` 与 1.2(b) 同下游。
- CLI 子命令 `cmd_add` 同入口(manager.py:8006 `cmd_add()` argparse 分派),路径相同。
- **多母号并行补齐**:`POST /api/tasks/multi-master/fill` → **api.py:3537** `post_multi_master_fill()` → `run_multi_master_fill` → **multi_master.py:243** 调 `cmd_fill(target, leave_workspace=False, post_sync=False)`。**不经 `post_fill` HTTP handler**,因此 fill 任务起点的 503 precheck 完全被绕过 —— 多母号路径的每个 owner 都裸跑注册。

---

## 2. 现有 master_health 闸位置盘点

### 2.1 fill HTTP 入口的 503 (✅ 已存在,但仅 fill 单 owner)
- **api.py:3473-3517**,在 `post_fill()` 内 inline 探测:`is_master_subscription_healthy(api)`(走 5min cache,可能空跑 cache 命中)。`subscription_cancelled` 时直接 503 拒绝建 task。
- 覆盖范围:HTTP `/api/tasks/fill`(leave_workspace=True/False 都覆盖)。
- 漏洞:
  1. 巡检后台直接 `_start_task(cmd_rotate / cmd_replace_batch)` 不经这一段(§1.2)。
  2. 手动 `/api/tasks/add`、`/api/tasks/replace`、`/api/tasks/multi-master/fill` 不经这一段(§1.3)。
  3. CLI `python -m autoteam fill / add / rotate` 不经 HTTP handler,完全不预检。

### 2.2 注册后 fail-fast (现有,产生风暴的根源)
- `_run_post_register_oauth` 内 **manager.py:3283** personal_precheck(leave_workspace=True 分支)与 **manager.py:3544** team_precheck(leave_workspace=False 分支)。
- 触发顺序:**5126 `create_temp_email` → 5206 `_register_direct_once`(整个浏览器注册 + 邮件 OTP + about-you)→ 进 `_run_post_register_oauth` → 3287/3548 行起 ChatGPTTeamAPI() + start + is_master_subscription_healthy + stop → 3297/3558 行 `subscription_cancelled` fail-fast**。
- 这就是日志(1).md 89-205 行 / 541-699 / 825-1019 行重复出现 "accountId 31→38 临时邮箱 → 浏览器注册 → master_subscription_degraded → kick → 丢弃" 的完整路径:浏览器和 CloudMail 已经消费完才回到 master probe。
- 注意:**probe 在闸位置上为时已晚,但实现本身依然有用** —— 它兜底了"创建邮箱后" master 状态突变的窗口,以及外层闸放行后(`network_error` / cache 过期)的二次校验。**不要删,只要加前置闸**。

### 2.3 没有覆盖的位置 — 三入口前置全空
| 入口 | 第一次访问 master_health 的位置 | 何时建邮箱 | 何时开浏览器 |
|---|---|---|---|
| 免费号批次(API) | api.py:3475(已 503) | 邮箱在 build 后开 task 才建 | 5206 行 _register_direct_once |
| 免费号批次(CLI) | **无前置**,直到 manager.py:3287 | 5126 行 | 5206 行 |
| 巡检 auto-fill | **无前置**,直到 manager.py:3548 / 3287 | 任务内 5126 行 | 5206 行 |
| 巡检 auto-replace | **无前置**,直到 manager.py:3548 | 6117 → 5126 行 | 5206 行 |
| 手动 /api/tasks/add | **无前置**,直到 manager.py:3548 | 5126 行 | 5206 行 |
| 手动 /api/tasks/replace | **无前置**,直到 manager.py:3548 | 5126 行 | 5206 行 |
| multi-master/fill | **无前置**(不经 post_fill),直到 manager.py:3548 | 每个 owner 各 5126 行 | 各 5206 行 |
| CLI add / rotate | **无前置**,直到 manager.py:3548 / 3287 | 5126 行 | 5206 行 |

---

## 3. 前置短路设计

### 3.1 设计原则
1. **预检走 cache**:`is_master_subscription_healthy` 默认 `cache_ttl=300s` 且 `cache 命中**不**发起 HTTP`(master_health.py:18 注释 + :313-391 cache 分支)。预检命中 cache 时几乎零开销,这是把闸前移的关键合理性。
2. **只对 `subscription_cancelled` fail-fast**,与现有 3297/3558 行语义一一对应。`network_error` / `auth_invalid` / `workspace_missing` / `role_not_owner` 都放行(避免与 r1-master-health 在查的"误判"修复冲突)。
3. **闸的归属位置 — 选最薄的瓶颈层**:统一插桩在 `create_account_direct`(manager.py:5415)入口,而不是每个入口都加。原因:
   - 它是 ALL 三入口的最终汇聚点(§1 的链路图都收敛于此)。
   - 任何调用方都会经过这一行 → 单点防御 = 全面覆盖。
   - "建邮箱(5126 行 in `_attempt_chatgpt_signup_only`)"在 5465 / 5467 调用之前,这一层加 probe 不会泄漏 CloudMail 配额。
4. **不替代 3287/3548 行兜底**:保留(承担 cache 过期 + network_error 转 healthy 后的二次校验)。

### 3.2 精确插桩点(伪代码)

**主插桩 — `create_account_direct` 入口预检** (manager.py:5415 之后,5458 `_resolve_mail_client_or_default` 之前):

```python
def create_account_direct(
    mail_client=None,
    *,
    leave_workspace=False,
    out_outcome=None,
    acc=None,
    path_rotator=None,
    parallel: int | None = None,
):
    """... existing docstring ..."""

    # ─────────────────────────────────────────────────────────────────────
    # Round 12 register-storm-gate: master 健康度前置短路 (cache-only,~0 RTT)
    # 防止"母号 cancel 但 cache 已 active=False"时整批仍跑 CloudMail+浏览器+OAuth。
    # 与 manager.py:3287/3548 的注册后 fail-fast 形成 defense-in-depth(双闸):
    #   - 前置闸:cache 命中即拒,不建邮箱不开浏览器
    #   - 注册后闸:兜底 cache 过期 / probe 网络抖动恢复后的二次验证
    # 仅拦 subscription_cancelled;auth_invalid / workspace_missing / role_not_owner /
    # network_error 一律放行(与 r1-master-health 修复保持兼容)。
    # ─────────────────────────────────────────────────────────────────────
    try:
        from autoteam.master_health import is_master_subscription_healthy
        from autoteam.chatgpt_api import ChatGPTTeamAPI
        from autoteam.register_failures import MASTER_SUBSCRIPTION_DEGRADED, record_failure

        probe_api = ChatGPTTeamAPI()
        try:
            probe_api.start()
            # cache_ttl=DEFAULT 300s,force_refresh=False — cache 命中 0 HTTP
            healthy, reason, evidence = is_master_subscription_healthy(probe_api)
        finally:
            try: probe_api.stop()
            except Exception: pass
    except Exception as probe_exc:
        logger.warning("[直接注册前置闸] master_health probe 异常 %s,按既有逻辑放行", probe_exc)
        healthy, reason, evidence = True, "active", {"detail": str(probe_exc)[:200]}

    if not healthy and reason == "subscription_cancelled":
        logger.error(
            "[直接注册前置闸] master 订阅 cancelled (cache_hit=%s),不建邮箱不开浏览器,fail-fast",
            evidence.get("cache_hit"),
        )
        # record_failure 写空 email — 没创建邮箱前没账号 ID,但仍要在 register_failures 留痕迹
        record_failure(
            "", MASTER_SUBSCRIPTION_DEGRADED,
            "master subscription cancelled (pre-register gate)",
            stage="create_account_direct_pre_register_gate",
            master_account_id=evidence.get("account_id"),
            master_role=evidence.get("current_user_role"),
        )
        if out_outcome is not None:
            out_outcome.clear()
            out_outcome.update(
                status="master_degraded",
                email="",
                reason="master subscription cancelled (pre-register)",
                master_evidence=evidence,
            )
        return None

    # ── 既有逻辑 ──
    if path_rotator is not None:
        return _create_account_direct_via_rotator(...)
    mail_client = _resolve_mail_client_or_default(mail_client, acc=acc)
    ...
```

**关键点**:
- `_attempt_chatgpt_signup_only` 在 5121 行调用 `mail_client.create_temp_email()`(5126),只要我们在 5417 行(`create_account_direct` 入口)就 short-circuit,5126 永远不会执行 → CloudMail 配额零消耗。
- 5121 内的 `_register_direct_once`(浏览器)在 5206 行,同理零开销。
- `_run_post_register_oauth` 在 5495 行,3287/3548 的兜底 probe 仍然存在(本批仍走完后续逻辑兜底)。

### 3.3 第二处插桩 — `_cmd_fill_personal` 批次启动期(可选,但推荐)

`_cmd_fill_personal` 一旦进入会 baseline-snapshot + 循环 BATCH。在循环开始前加一次 cache probe,如果 `subscription_cancelled` 直接 abort 整个 fill-personal,避免连续 BATCH 反复创建 ChatGPTTeamAPI 实例。

精确位置:**manager.py:7400 前**(`api_snap = _ensure_chatgpt()` 这一段紧邻),复用 `_ensure_chatgpt()` 拿到的 api 实例直接 probe(同一句 ChatGPTTeamAPI 既给 baseline snapshot 又给 master probe,零额外 start/stop):

```python
# 7400 行起 api_snap = _ensure_chatgpt() 后
try:
    from autoteam.master_health import is_master_subscription_healthy
    from autoteam.register_failures import MASTER_SUBSCRIPTION_DEGRADED, record_failure
    healthy, reason, evidence = is_master_subscription_healthy(api_snap)
except Exception as exc:
    logger.warning("[免费号] 启动前 master_health probe 异常 %s,放行", exc)
    healthy, reason, evidence = True, "active", {"detail": str(exc)[:200]}

if not healthy and reason == "subscription_cancelled":
    logger.error("[免费号] master 订阅 cancelled,整批 fill-personal 不启动 (cache_hit=%s)",
                 evidence.get("cache_hit"))
    record_failure(
        "", MASTER_SUBSCRIPTION_DEGRADED,
        "master subscription cancelled (fill-personal batch gate)",
        stage="cmd_fill_personal_batch_gate",
        master_account_id=evidence.get("account_id"),
    )
    _stop_chatgpt()
    return  # 整轮 cmd_fill_personal 不进 while 循环
```

### 3.4 第三处插桩 — `_replace_single` 进 create_new_account 前(精确补漏)
- `_replace_single` 在 6117 行才到 `create_new_account` → §3.2 的 `create_account_direct` 已经拦住,无需额外插桩。
- **但** 6000-6011 行 `_replace_single` 一上来就先 kick 失效账号 → 标 STANDBY。如果 master 已 cancel,kick 是无效操作(席位会被前置闸阻止新号补位,等于裸 kick)。建议在 6001 行前加一次 cache probe,cancelled 时直接 outcome=master_degraded 返回,不做 kick:

```python
# 6000 行 `logger.info("[替换] kick ...` 之前
try:
    from autoteam.master_health import is_master_subscription_healthy
    healthy, reason, _evidence = is_master_subscription_healthy(chatgpt)
except Exception:
    healthy, reason = True, "active"
if not healthy and reason == "subscription_cancelled":
    outcome["error"] = "master_degraded"
    logger.warning("[替换] master 订阅 cancelled,不 kick 不补位: %s", email)
    return outcome
```

注意此处复用调用方传入的 chatgpt 实例,不需再 start/stop。

### 3.5 调用方 outcome 兼容
- `out_outcome["status"] = "master_degraded"` 与 manager.py:3310 / 3578 现有语义一致;
- `_cmd_fill_personal` 的 `_summarize_outcomes`(manager.py:7202)按 status 字段聚合,新增 master_degraded key 自动被计入,前端可显示 "本批因母号订阅取消未启动 N 个"。

---

## 4. 与 r1-master-health 的接口约定

r1 在查 master_health 的"误判"问题。我们的前置闸**必须**遵守以下契约,以避免和 r1 修复冲突:

1. **只 fail-fast `reason == "subscription_cancelled"`**。其余 reason(`network_error` / `auth_invalid` / `workspace_missing` / `role_not_owner` / `subscription_grace`)都不在前置闸阻拦范围,与 manager.py:3312-3321 现有语义对齐。
2. **使用默认 cache(`force_refresh=False, cache_ttl=300`)**。前置闸是"轻量+频繁"路径,不允许 force_refresh —— 否则会出现"前置闸 force_refresh 把 cache 强制设为 cancelled,后续兜底 probe 又看到 cache 直接拒"的双重锁死。r1 修复后,只要 cache 项不再被错误打成 cancelled,前置闸自然放行。
3. **保留 manager.py:3287/3548 兜底闸**。如果 r1 修复期间出现"cache hit 是错的,但下次实测会更新",前置闸放过 → 注册成功 → 兜底闸用真值(cache 已被 L1 探针刷新)再次判定。双闸总能在两轮内收敛到正确状态。
4. **绝不在前置闸调 `apply_pool_health_signal`**。pool failover 是远端 workspace 切换,频率必须低。前置闸只读 cache,不上报 pool signal(api.py:1633 在 force_refresh 路径才上报)。
5. **若 r1 引入新 reason**:只要 master_health.py 在新 reason 上正确设置 healthy=False,前置闸的 `reason == "subscription_cancelled"` 字符串比对会自动放行新 reason,等用户/r1 在 manager.py 决定如何兜底。

---

## 5. 对照 `.upstream/manager.py` 注册流程顺序差异

上游 `.upstream/manager.py` 与本地差异显著:

| 维度 | `.upstream/manager.py` | 本地 `src/autoteam/manager.py` |
|---|---|---|
| `create_account_direct` 行号 | 2030 | 5415 |
| 是否引入 master_health | **完全没有** master_health.py 模块 | 有完整 master_health 体系(5min cache + L1/L3 探针) |
| 注册前 / 后 fail-fast | 都没有,跑完 OAuth 看 plan_type 不对就 `_record_auth_repair_failure` | 注册前(本提案)+ 注册后兜底(3287/3548) |
| 邮件 provider | 单一 cloudmail,简单 retry | path_rotator + provider chain |
| 注册后 OAuth 收尾 | `_login_codex_with_result`(2080) + 简单 plan_type 校验 | `_run_post_register_oauth` 含 personal/team 双分支 + 5 次重试 + sticky-rejoin |
| plan_type=team 才算成功 | 是(reinvite_account 2160 行 `if plan_type != "team"`) | 是(白名单 + plan_supported,saner) |
| 母号 cancel 防护 | **无** — 跑完 OAuth 看 plan=free 就 record_failure | 现有注册后 + 本提案前置 |
| `cmd_fill` 默认 target | 5(2759 行) | 3(7082 行,与 TEAM_SUB_ACCOUNT_HARD_CAP 一致) |

**结论**:上游根本没有 master_health 概念,本地的体系本来就是新增防御层。本提案不影响上游兼容性 —— 加在本地 manager 的 create_account_direct 入口,完全是本地资产。

---

## 6. 测试点(round-12 禁止真实 e2e,以下都是静态分析/单测可验)

1. **单测 1 — cache hit cancelled 阻断**:在 `_attempt_chatgpt_signup_only` mock 之前 stub `is_master_subscription_healthy` 返回 `(False, "subscription_cancelled", {...})`,验证 `create_account_direct` 直接 return None 且 `mail_client.create_temp_email` 未被调用(mock assert_not_called)。
2. **单测 2 — cache hit network_error 放行**:stub 返回 `(False, "network_error", {})`,验证仍然走到 `_attempt_chatgpt_signup_only` 并最终命中 manager.py:3287 兜底闸(即旧行为不变)。
3. **单测 3 — out_outcome 透传**:stub 返回 cancelled,传 out_outcome={},验证返回后 `out_outcome["status"] == "master_degraded"`。
4. **单测 4 — _summarize_outcomes 计数**:构造若干 outcome dict 含 `status: "master_degraded"`,验证 `_cmd_fill_personal` 末尾的 `_summarize_outcomes(outcomes)` 计入 master_degraded key。
5. **单测 5 — `_replace_single` 不 kick**:stub master_health 返回 cancelled,验证 `remove_from_team` 未被调用,outcome 返回 `error="master_degraded"`。
6. **静态对照**:用 `git grep "create_account_direct\|create_temp_email"` 验证所有调用路径都过得了新闸(全部走 manager.py:5417 入口)。
7. **日志重现**:对照 `D:/Desktop/AutoTeam/resource/日志(1).md` 89-205 行的 master_subscription_degraded 链路,确认插桩后 "accountId 31→38" + "_register_direct_once" 的浏览器/邮件序列在前置闸生效时不会出现。

---

## 7. 风险与回退

- **风险 A**:cache 与真值短时间不一致(cache_ttl=300s)。回退手段:用户在 UI 点"立即重测"调 `/api/admin/master-health?force_refresh=1` 刷新 cache,前置闸下一调用立刻看到正确值。本提案不引入新 cache。
- **风险 B**:probe 调用本身抛异常(网络 / start 失败)。已用 try/except 兜底为 `healthy=True, reason="active"`,放行,等同于关闸 → 由 manager.py:3287/3548 兜底。
- **风险 C**:CLI 调用方拿不到 HTTP 503 信号。本提案 record_failure + out_outcome 完整,CLI 看到 logger.error + return None,与现有"注册失败"分支一致。
- **回退**:若新闸误拦,只需移除 §3.2 / §3.3 / §3.4 三段代码,行为退化为现状。**不会**破坏已有 manager.py:3287/3548 兜底闸。

---

## 8. 置信度

- **入口三处链路**:置信度 95%(基于 file:line 直接 grep 验证)。
- **现有闸位置(注册后)**:置信度 99%(直接读 manager.py:3299/3560 + api.py:3473 代码确认)。
- **前置闸设计(create_account_direct 入口)**:置信度 90%(架构合理,但未跑真实 e2e —— round-12 禁令)。
- **与 r1 协调约定**:置信度 75%(取决于 r1 修复方向,本设计已给出回退/兼容 5 条契约)。
- **测试点可执行性**:置信度 85%(单测可写,完整 mock chain 在 stub master_health 后即可)。

---

## 9. 关键 file:line 摘要(供 PRD / implementer 直接复制)

| 用途 | file:line |
|---|---|
| 前置闸**主插桩**位置 | `src/autoteam/manager.py:5417` (`def create_account_direct` 开头,在 `_resolve_mail_client_or_default` 之前) |
| 前置闸**批次插桩**(可选) | `src/autoteam/manager.py:7400` (`_cmd_fill_personal` 内 `api_snap = _ensure_chatgpt()` 之后,while 循环之前) |
| 前置闸**替换插桩**(精确) | `src/autoteam/manager.py:6000` (`_replace_single` kick 调用之前) |
| 现有 fill HTTP 503 | `src/autoteam/api.py:3473-3517` (不动) |
| 现有 personal 注册后兜底 | `src/autoteam/manager.py:3283-3322` (不动) |
| 现有 team 注册后兜底 | `src/autoteam/manager.py:3544-3580` (不动) |
| master_health 主探针 | `src/autoteam/master_health.py:313` (`is_master_subscription_healthy`) |
| MASTER_SUBSCRIPTION_DEGRADED 常量 | `src/autoteam/register_failures.py:33` |
| 风暴热点(临时邮箱创建) | `src/autoteam/manager.py:5126` (`mail_client.create_temp_email()` in `_attempt_chatgpt_signup_only`) |
| 风暴热点(浏览器注册) | `src/autoteam/manager.py:5206/5217` (`_register_direct_once`) |
| auto-check 触发 cmd_rotate | `src/autoteam/api.py:4267-4274` |
| auto-check 触发 cmd_replace_batch | `src/autoteam/api.py:4333-4342` |
| auto-check 触发 cmd_rotate(provider-auth) | `src/autoteam/api.py:4368-4383` |

---

## 10. 一行结论

把"cache-only `is_master_subscription_healthy` cancelled 短路"装进 `create_account_direct`(manager.py:5417) + `_cmd_fill_personal`(manager.py:7400) + `_replace_single`(manager.py:6000) 三处,即可在不破坏 r1 修复的前提下让母号 cancel 时三入口的整批注册零成本拒绝,与现有 manager.py:3287/3548 注册后兜底形成 defense-in-depth 双闸。
