# fix: master 订阅 cancel 后注册风暴与重试延迟异常

## Goal

当 farmer1 母号(account_id=105c3bb4-4fcc-4f37-9f6b-a5afe1ab8d30)的 ChatGPT Team
订阅被 cancel(`eligible_for_auto_reactivation=true`,新 invite 强制 `plan_type=free`)后,
当前 `D:\Desktop\AutoTeam` 出现一系列异常行为(见 `resource/` 日志+截图)。
用户要求**比照旧的已知良好快照 `D:\Desktop\autoteam-1` 的行为**定位并修复回归。

## What I already know (from resource/ 日志 + 截图)

错误素材:`resource/日志(1).md`(05-31 20:43→21:51 运行日志)、两张面板截图。

面板状态:
- 顶部 ALERT:**母号订阅已 cancel · 请续费或切换母号**;`eligible_for_auto_reactivation=true`,
  `role=account-owner`,`http=200`,`fill-personal 入口已 503 拒绝`,上次探测 05-31 21:05:09 (cache)。
- 账号池 3 个,**0% 可用**:2 个"认证失效/席位异常",1 个 DISABLED(已从自动化流程排除)。
- 注册失败明细:`register_failed: 3`、`oauth_phone_blocked: 2`、`master_subscription_degraded: 2`。

日志暴露的 4 个症状:

1. **注册风暴 / 资源空转(主症状)** — master 已 cancel 后,免费号批次 + 巡检 + 手动添加仍反复:
   建 CloudMail 临时邮箱(accountId 31→38)→ 跑完整浏览器注册(email→code→about-you)→ 入 workspace →
   **直到 `run_post_register_oauth_team_precheck` 才 fail-fast 为 `master_subscription_degraded`** →
   kick 席位 → 丢弃账号 → 删临时邮箱。每次都白烧 CloudMail 配额 + 浏览器时间 + OpenAI 速率预算,产出为 0。
   `master_health.is_master_subscription_healthy()` 已有 cache 预检(命中不发 HTTP),但注册流程里这道闸**触发得太晚**(注册后才查)。

2. **360018 分钟重试延迟(明显 bug)** — 日志 779 行:
   `[96a6a05b15] Codex 登录失败,标记为 auth_invalid(登录异常,约 360018 分钟后重试)`。
   360018 分钟 ≈ 250 天。`_record_auth_repair_failure` 写的 `auth_retry_after` 落在 ~21.6M 秒后。
   重试延迟来自 `_auth_repair_retry_delays()`/`_auth_repair_add_phone_retry_delays()`(基于巡检 interval),
   正常应是分钟级,疑似回归 / 数值溢出 / 单位错误。

3. **OAuth add-phone / 拿不到 auth code** — `4abf4a6d8b`(personal OAuth→add-phone @oauth_about_you)、
   `14bc83d28b`(Team OAuth→add-phone @oauth_consent_1)、`bf7be99f48`(Team OAuth→未获取到 auth code)。
   疑似 OpenAI 反爬升级(与 memory `project_openai_antibot_2026_04_30` 一致),未必是代码回归。

4. **主号 Codex 认证文件刷新失败** — 日志 529 行:admin 登录成功但
   "无法基于管理员登录态生成主号 Codex 认证文件"(session OAuth 卡在 code_required)。
   注意:此事件(20:53)发生后,后续注册才开始被判 master_degraded —— 需确认二者因果。

## 两仓关系(已确认)

- **当前 `D:\Desktop\AutoTeam`** = 活跃开发 HEAD(git main),新增 `master_health.py`/`register_failures.py`/
  `multi_master.py`/`workspace_pool.py`/`account_state.py` 等,manager.py 大量注释指向 `.upstream/`。
- **`D:\Desktop\autoteam-1`** = 旧独立快照(accounts/state 停在 05-14~15,**非 git 仓库**),
  **无** master_health/register_failures,但保留 auth-repair 重试逻辑(`auth_retry_after`/add_phone 退避/`_record_auth_repair_failure` 共 30 处命中)。
- 当前仓库内 `.upstream/`(manager/chatgpt_api/codex_auth/invite/oauth_workspace)= vendored 上游参考。

## 用户已确认(2026-06-01)

* **Q1 → 母号"没有 cancel"。** 即面板上的"订阅已 cancel"是**误判 (false positive)**。
  健康母号(http=200、role=account-owner、eligible_for_auto_reactivation=true)被新 `master_health`
  分类错判成 cancelled/degraded,进而触发 fail-fast 注册风暴 + fill-personal 503。
  **这是头号根因。** autoteam-1 无此分类闸,所以"能正常跑"。
* **Q2 → 全量排查。** 4 类症状全查,逐项比照 autoteam-1/.upstream 找回归。
  受 round-12 约束:验收靠代码静态分析 + 对照,**禁止真实 e2e**(见 memory)。
* **Q3(范围深度)→ 全做 PR1–PR4**(含 R6 consent loop 自恢复 + session bundle fallback 深度移植)。
* **Q4(下一步)→ 立即进入实施**。task.py start 后由 lead 按 PR 顺序串行推进(R3/R4/R5 同改 manager.py、R2/R6 同改 codex_auth.py,文件交叠不并行)。

## 修订后的头号假设

* `eligible_for_auto_reactivation=true` 被分类逻辑当成"已取消"是错的 —— 该字段对健康订阅也可能为 true,
  或 20:53 admin 重登(主号 Codex 认证刷新失败)污染了 `.master_health_cache.json`。待 research 证实。

## Requirements (final)

* [ ] **R1(P0,根因)** `master_health._classify_l1` 改为三字段 AND 判定(`eligible_for_auto_reactivation` ∧ `is_deactivated` ∧ `has_active_subscription is False`)才判 `subscription_cancelled`;否则视为 active。健康母号(is_deactivated=false)不再被误判。
* [ ] **R1b(P0 辅)** admin 重新登录(`api.py` cmd_admin_login)完成时 invalidate 该 account_id 的 `.master_health_cache.json` 项;`subscription_cancelled` 加 hysteresis 或不写 cache,避免单次假阳性锁定 5 分钟。
* [ ] **R2(P0)** `codex_auth._build_auth_url` 对齐 autoteam-1:`prompt=get_codex_authorize_prompt()`(默认 `login`,env `CODEX_AUTHORIZE_PROMPT` 可回退)+ `id_token_add_organizations=true` + `codex_cli_simplified_flow=true`。
* [ ] **R3(P1,防御)** 在 `create_account_direct`(manager.py:5417)前置 cache-only 母号健康短路(仅拦 `subscription_cancelled`,默认 cache,不 force_refresh),可选追加 `_cmd_fill_personal`/`_replace_single` 两处;保留现有注册后兜底闸(双闸)。
* [ ] **R4(P1)** `_call_login` 不再把 `RegisterBlocked` 吞成 `exception`:按 `is_phone/is_duplicate` 映射 `add_phone/duplicate_email/human_verification`,`retryable=False` 立即 break(不白跑 3 次);UI/日志显示正确中文标签。
* [ ] **R5(P1)** 退避加 24h hard cap(`_clamp_retry_delay`),覆盖 `_auth_repair_retry_delays`/`_auth_repair_add_phone_retry_delays`;`set_auto_check_config` 的 interval 入口加 ceiling 防爆。
* [ ] **R6(可选,深度)** 移植 autoteam-1 consent loop 自恢复(login-challenge/oauth_timeout/no_valid_org)+ session bundle fallback(降低"拿不到 auth code")—— 较大,见 scope 决策。

## Acceptance Criteria (final)

* [ ] **AC1** 给 `_classify_l1` 喂"现场夹具"(eligible=true, is_deactivated=false, has_active_subscription=true, plan_type=team)→ 返回 `(True, "active")`(当前返回 `(False, "subscription_cancelled")`)。真 cancel 夹具(三字段全翻)仍返回 cancelled/grace。
* [ ] **AC2** `_build_auth_url()` 返回 URL 含 `prompt=login`(默认)+ 两个新参数;env `CODEX_AUTHORIZE_PROMPT=consent` 时回退 consent。
* [ ] **AC3** stub `is_master_subscription_healthy → (False,"subscription_cancelled")` 时 `create_account_direct` 直接 return,`mail_client.create_temp_email` 未被调用(assert_not_called);`network_error` 等其他 reason 放行。
* [ ] **AC4** `_call_login` 收到 `RegisterBlocked(is_phone=True)` → `error_type="add_phone"` 且 `retryable=False`,外层 attempt loop 立即 break。
* [ ] **AC5** interval=999999s(脏)时 `_auth_repair_retry_delays` 最大档 ≤ 24h;不再出现 `约 NNNN 分钟后重试`(>1 天)。
* [ ] **AC6** 既有测试 + 上述新增单测全绿;lint/typecheck 绿。round-12:**仅静态分析 + 单测**,禁止真实 e2e。

## Implementation Plan(小 PR 拆分)

* **PR1(P0 止血)✅ 完成** :R1(master_health 三字段 AND)+ R1b(invalidate_cache+admin 重登接入)+ R2(prompt=login)。tests 全绿,ruff 干净,spec 加 M-I18/M-I19。
* **PR2(P1 防御)✅ 完成** :R3 注册前置闸 —— 装在唯一汇聚点 `create_new_account`(覆盖 direct+invite+免费号/巡检/add/replace/fill-personal),只拦 cancelled、用 live session 或 cache-only probe,保留注册后兜底闸构成双闸。67 注册/manager 测试通过。
* **PR3(P1 退避)✅ 完成** :R4(_call_login 不吞 RegisterBlocked,add_phone/duplicate/human_verification + retryable=False 立即 break)+ R5(_clamp_retry_delay 24h 硬封顶 + set_auto_check_config interval 入口防爆)。95+17 测试通过。
* **PR4(本轮纳入)⏳ 待办** :R6 consent loop 自恢复(login-challenge/oauth_timeout/no_valid_org)+ session bundle fallback,移植自 autoteam-1 `codex_auth.py`。范围大、且 round-12 无账号**无法 e2e 验证**浏览器交互 —— 见下方"实施状态/PR4 决策"。

## 实施状态(2026-06-01)

PR1–PR3 已落地并通过静态验证(全量单测 916 通过;受影响子集逐一复跑全绿;ruff 干净)。
改动文件:`master_health.py`、`api.py`、`config.py`、`codex_auth.py`、`manager.py` + 4 个测试文件 + spec。
**未提交**(等用户 review)。PR4 因 round-12 无法 e2e、且属浏览器自动化大块移植,留待用户决定是否本轮强推或单列。

## Definition of Done

* 针对性单测(retry 延迟边界、master-degraded 注册短路)新增/更新
* lint / typecheck / 测试 green
* 行为变更记入 spec / 注释(标注与 autoteam-1/.upstream 的差异)
* round-12 无可用账号约束:验收靠代码静态分析 + 对照,禁止真实 e2e(见 memory)

## Out of Scope (tentative)

* 帮用户续订 / 切换母号(运营动作,非代码)
* 绕过 OpenAI add-phone 反爬(若确认为外部反爬而非回归)

## Research References(team `mh-fp-research` 产出,2026-06-01)

* [`research/master-health-fp.md`](research/master-health-fp.md) — **头号根因**:`_classify_l1` 单字段 `eligible_for_auto_reactivation=true` 误判为 cancelled;修复=三字段 AND(+ admin 重登清 cache)。置信 High。
* [`research/oauth-codex.md`](research/oauth-codex.md) — **本地回归**:`codex_auth._build_auth_url` 用 `prompt=consent`,autoteam-1 早已切 `prompt=login`(+`id_token_add_organizations`/`codex_cli_simplified_flow`),是 add-phone 高发直接根因;consent loop 缺自恢复。`refresh_main_auth_file` 失败与 master_degraded **不耦合**。置信 ≥90%。
* [`research/register-storm-gate.md`](research/register-storm-gate.md) — **防御缺口**:三注册入口在建邮箱/开浏览器前无母号健康闸;前置 cache-only 短路装进 `create_account_direct`(manager.py:5417)+ `_cmd_fill_personal`(:7400)+ `_replace_single`(:6000),与现有注册后兜底构成双闸。置信 90%。
* [`research/retry-delay.md`](research/retry-delay.md) — **非本地回归(三方同源)**:`_call_login` 把 `RegisterBlocked(add-phone)` 吞成 `error_type="exception"`(BUG#1,丢 add_phone 信号+白跑 3 次);退避无 ceiling(BUG#2)。360018 精确算式需 runtime dump,纯静态无法锁定,但修复后该症状无论来源都不再出现。置信:BUG#1/#2 High,精确数学 Mid。

## Decision (ADR-lite)

**Context**:面板报"母号订阅已 cancel"触发注册风暴 + 250 天重试 + 0 可用账号。用户实证**母号其实没 cancel**,要求比照旧快照 autoteam-1 修复。team 四专题并行排查后定位四条独立缺陷。

**Decision**:四缺陷全量修,但分层处理(下方 Implementation Plan)。关键判断:
- 根因(P0)= master_health 单字段误判 → 三字段 AND 判定(r1 Option A)。这是"0 可用账号"的总开关。
- add-phone 风暴(P0)= `prompt=consent` 回归 → 对齐 autoteam-1 的 `prompt=login`。低风险高收益,且正是"比照 autoteam-1"的直接产物。
- 注册风暴前置闸(P1)= defense-in-depth,即便根因修了仍保留(真降级时不空转)。
- 重试延迟(P1)= 不吞 RegisterBlocked + 24h 封顶 + interval 入口防爆。**注意:此项三方同源,非回归,autoteam-1 无可对照差异,是净新增防护。**

**Consequences**:
- 三字段 AND 在 OpenAI 不返回 `is_deactivated`/`has_active_subscription` 时会退化为"偏向 active"(更保守,只会减少误杀,真降级由注册后兜底闸 + OAuth 失败兜底接住)——与"autoteam-1 无闸也能跑"一致,可接受。
- `prompt=login` 切换提供 env `CODEX_AUTHORIZE_PROMPT` 可回退到 consent。
- consent loop 自恢复 / session bundle fallback 是较大移植(r4 建议项),作为可选深度,见下方 scope 决策。
- 360018 精确来源未 100% 锁定;以"封顶 + 防爆 + 不吞信号"使症状不可复现,并加 logging 便于 round-13 真账号时复盘。

## Technical Notes

错误素材:
- `resource/日志(1).md`、`resource/360f723…png`、`resource/6ecde74…png`

关键代码位(当前仓库):
- 重试延迟:`src/autoteam/manager.py:747 _auth_repair_retry_delays`、`:802 _auth_repair_add_phone_retry_delays`、
  `:860 _auth_repair_state_suffix`(拼"约 N 分钟后重试")、`:951 _record_auth_repair_failure`(写 auth_retry_after)、
  `:2606` auth_invalid 标记日志。
- master 降级:`src/autoteam/master_health.py:313 is_master_subscription_healthy`(带 cache,命中不发 HTTP)、
  `_apply_master_degraded_classification`;manager.py:3299 personal OAuth fail-fast、:3560 Team OAuth fail-fast。
- 注册入口(免费号批次 / 巡检 / 加号)闸位置待 research 定位。

对照基线:
- `D:\Desktop\autoteam-1\AutoTeam\src\autoteam\manager.py`(旧 auth-repair 逻辑)
- `D:\Desktop\AutoTeam\.upstream\manager.py`(vendored 上游)
