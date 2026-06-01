# 360018 分钟异常 retry 延迟 — root cause 与修复

## TL;DR (置信度)

- **置信度 高**: 错误分类 bug — `_call_login` 把 `RegisterBlocked(add-phone)` 异常吞掉,降级成 `error_type="exception"`,导致 (a) 同一个 add-phone 永久阻塞跑满 3 attempts;(b) 在 `_record_auth_repair_failure` 走错分支(else 衰退式三档,而非 `add_phone` 指数退避或 hard failure)。
- **置信度 高**: 退避封顶缺失 — `_auth_repair_retry_delays()` 与 `_auth_repair_add_phone_retry_delays()` 直接信任 `_auto_check_config["interval"]`,**只 floor 60s 没有 ceiling**;调用方传入(或上游 UI 写入)任何 N 都会被 ×2 / ×4 / ×6 / ×2^idx 放大。
- **置信度 中**: 21,601,080 秒(=360018 min)≈ interval×12000(干净)≈ 1800×2^13.55(脏),数学上**唯一干净落点**是 `interval=3,600,180s × 6`(==else 分支 `retry_delays[2]`),或 `interval×2^idx` 在 idx≈13.55 的近似。日志显示运行时 `interval=1800s`,所以这不是用户主动配置;最可能是 (i) `_auto_check_config["interval"]` 被另一段代码意外覆盖成 ms/μs/minute-treated-as-seconds 的脏值,或 (ii) `add_phone_max_retries` 被环境变量设置成 ≥14,触发 `1800*2^13≈14.7M` 量级。**仅靠 log+代码无法完全锁定**,需要 dump 当时的 accounts.json + 环境变量。

---

## 触发流水(基于 `resource/日志(1).md`)

```
20:59:05  注册成功 → invite Team 成功 → master_degraded fail-fast,
          直接 update_account(status=AUTH_INVALID),**未走 _record_auth_repair_failure**,
          故 auth_retry_count 仍为 0。
21:01:36  用户从 UI 把 interval 从默认 → 1800s(`_auto_check_config["interval"]=1800`)。
21:08:28  二次 OAuth(可能是手动 retry 或 reconcile pickup),session_token=no。
21:09:29  oauth_consent_1 探测命中 add-phone → invite.assert_not_blocked 抛 RegisterBlocked
          → login_codex_via_browser 不 catch → 冒到 manager._login_codex_with_result._call_login
          → except Exception: return {error_type: "exception", retryable: True}
          → ⚠️ 这是 BUG#1:add-phone 信号被擦成 "exception"。
21:09:29  HTTP 409 phone_required 返回前端,任务标 oauth_phone_blocked failed。
          (此路径未必走 _record_auth_repair_failure — Task 1 是 add-account 上层吞了。
           需 verify:add_account 末尾若不调 _record_auth_repair_failure,则 auth_retry_count 仍是 0。)
21:43:15  WARNING "本轮重试第 3/3 次"(_login_codex_with_result attempt 2 失败,即将 attempt 3)
          → 说明这一次外层流程**重新进入 `_login_codex_with_result(max_attempts=3)`**;
            attempt 1/2 都是 "exception" retryable=True,被吃掉,attempts 1/2 没有显式 log。
          → ⚠️ BUG#1 的二次后果:整整 3 个 attempts 都白跑,每次都打开 Playwright 走完
            注册站到 oauth_consent_1 才探出 add-phone,极度浪费。
21:44:18  attempt 3 又命中 add-phone → 同上 "exception"。
21:44:20  manager.py:2607 ERROR
          [96a6a05b15@medu1a.moeflux.com] Codex 登录失败,标记为 auth_invalid
          (登录异常,约 360018 分钟后重试)
          ↑ 即 `_record_auth_repair_failure(email, error_type="exception", ...)` 返回的
            `_auth_repair_result_suffix(result)` 的输出。
```

---

## `_record_auth_repair_failure` 分支决策 — 为什么落在 else

`error_type="exception"`,代码 `src/autoteam/manager.py:950-1060`:

```python
if _should_aggressively_release_auth_failure("exception", discard_failed_repair=True):
    → 命中:"exception" 在 AUTH_REPAIR_AGGRESSIVE_RELEASE_TYPES (manager.py:286)
    → 分支 1:auth_retry_after=None, paused=True, release_team_seat=True
elif error_type == "add_phone" and ...:  → 跳过
elif error_type in AUTH_REPAIR_HARD_FAILURE_TYPES or "add_phone": → 跳过
elif error_type in AUTH_REPAIR_RELEASE_TEAM_BLOCKER_TYPES and ...: → 跳过
else:
    → 衰退式三档:auth_retry_after = now + retry_delays[min(prev_count, 2)]
```

**关键反证**: 如果 `discard_failed_repair=True`(`ROTATE_SKIP_REUSE=True` 默认)+ `"exception"` 在 AGGRESSIVE set → 分支 1 命中 → `auth_retry_after=None`, `auth_retry_paused=True`。
→ `_auth_repair_state_suffix` 应该输出 "已暂停自动修复"(`manager.py:863-864`),而不是 "约 N 分钟后重试"。

但日志显示后缀是 "约 360018 分钟后重试" → **分支 1 没有命中**。
→ 推论:`ROTATE_SKIP_REUSE=False`(用户 env 覆盖了默认),`_should_aggressively_release_auth_failure` 直接 return False(`manager.py:942-943`),落到 else 分支(`manager.py:1044-1056`)。

else 分支:
```python
prev_count = int(acc.get("auth_retry_count") or 0)
next_count = min(prev_count + 1, len(retry_delays))   # cap 3
delay = retry_delays[max(0, next_count - 1)]          # retry_delays = (i*2, i*4, i*6)
retry_after = now + delay
```

**理论最大延迟**: `interval×6`。interval=1800 → max=10800s=180min。**远低于 360018**。

---

## 21,601,080 秒来源 — 数学反推

| 假设 | 公式 | 算出 | 是否干净 |
|---|---|---|---|
| else 分支 + interval=1800 | 1800 × {2,4,6} | 3600 / 7200 / **10800** | ✗ (差 2000x) |
| else 分支 + interval=3,600,180 | 3,600,180 × 6 | **21,601,080** ✓ | ✓ 干净 |
| add_phone 分支 + interval=1800 + max_retries=N | 1800 × 2^(N-1) | 2^idx=12,000.6 → idx≈13.55 | ✗ (非整数指数) |
| add_phone 分支 + interval=1800 + max_retries=14 | 1800 × 2^13 | 14,745,600 (245,760min) | ✗ |
| add_phone 分支 + interval=1800 + max_retries=15 | 1800 × 2^14 | 29,491,200 (491,520min) | ✗ |

**唯一干净落点**: else 分支 + interval≈3,600,180s + idx=2(即 `prev_count≥2`)。

interval=3,600,180s ≈ 41.67 天 ≈ 60003 分钟。这不是 UI 显式配的(UI 是 1800),也不可能是合理 env。
→ 推断:**`_auto_check_config["interval"]` 被某个内部 helper 写脏了**,可能场景:
  1. 某 follow-up 流程把 `quota_resets_at` 风格的"future absolute UNIX ts"误存进 `interval`(`set_auto_check_config` 入口 `max(60, cfg.interval)` 不阻挡)。
  2. 前端 UI 把 "interval_minutes" 字段误传成 `interval`(但 21:01:36 log 已经 echo 1800 而非 60003,所以这条不太可能在 21:01 之后发生)。
  3. (低概率)`_call_login` 落进 `except Exception` 后,某段 cleanup 错误地把 `auth_retry_count` 累加到 13+,导致 add_phone 分支的 `2**13` 退避(但 add_phone 分支需要 `error_type=="add_phone"`,而 BUG#1 已把 error_type 改成 "exception",所以这条**不会触发**)。

⚠️ **本地静态分析不足以 100% 锁定** 来源,需要(round-12 不能 e2e 时)在测试夹具里 dump:
  - 21:44:20 之前的 `accounts.json` 中该 email 的 `auth_retry_count` 字段;
  - 当时的 `_auto_check_config` dict(可通过 API `/api/config/auto-check` GET 抓);
  - `AUTO_CHECK_INTERVAL` env 实际值。

---

## 当前 vs autoteam-1 vs .upstream 差异

| 文件 | else 分支 delay 算式 | add_phone 分支 delay 算式 | 是否封顶 |
|---|---|---|---|
| `src/autoteam/manager.py:1047` | `retry_delays[next_count-1]` (= i×2/4/6) | `add_phone_delays[next_count-1]` (= i×2^idx) | **无 ceiling** |
| `D:/Desktop/autoteam-1/AutoTeam/src/autoteam/manager.py:1398` | 同上 | 同上 | **无 ceiling** |
| `.upstream/manager.py:401` | 同上 | 同上 | **无 ceiling** |

→ 三个版本**完全同源**,本地相对 autoteam-1 没引入回归。  
→ 但三者**共同**的封顶缺失才是真正的根因,**不是本地回归**。

## BUG#1 — RegisterBlocked → "exception" 降级

`src/autoteam/manager.py:1170-1177` 的 `_call_login`:

```python
except Exception as exc:
    return {
        "ok": False,
        "bundle": None,
        "error_type": "exception",
        "error_detail": str(exc),
        "retryable": True,
    }
```

这里 `RegisterBlocked`(`autoteam.invite.RegisterBlocked`)被 Exception 大网捞走,**未区分 `add-phone` / `duplicate email` / `human verification`**。

后果:
1. 把 `error_type="add_phone"` 永久失去 → `_record_auth_repair_failure` 永远不进 add_phone 指数退避或 hard-failure 分支;
2. `AUTH_REPAIR_SINGLE_ATTEMPT_FAILURE_TYPES` 检查(`manager.py:1199`)失效 — 应该 attempt=1 就 break,实际跑满 max_attempts=3;
3. UI / 日志的中文标签从 "手机号验证" 错误显示为 "登录异常",运维误判。

`.upstream/manager.py:486-493` 也有相同 bug — 来自 upstream cherry-pick 的固有缺陷。

## BUG#2 — retry_after 退避无封顶

即使 BUG#1 修了 add_phone 走对了分支:
- add_phone 分支:`add_phone_delays = (interval*2^0, ..., interval*2^(N-1))`,如果用户 env 把 `AUTO_CHECK_ADD_PHONE_MAX_RETRIES` 调到 10+,delay 仍可能 ≥ 1 天。
- else 分支:`retry_delays = (interval*2, interval*4, interval*6)`,只要 `_auto_check_config["interval"]` 被脏数据污染,delay 就会爆。

没有"无论怎么算都不超过 X 小时"的最终 clamp。

---

## 修复方案(按风险 / 收益排序)

### Fix A(必做,高优先)— 别把 RegisterBlocked 当 generic Exception

`src/autoteam/manager.py:1170` 之前增加分支:

```python
from autoteam.invite import RegisterBlocked

try:
    result = login_codex_via_browser(email, password, **kwargs)
except TypeError:
    ...
except RegisterBlocked as blocked:
    if blocked.is_phone:
        et = "add_phone"
    elif getattr(blocked, "is_duplicate", False):
        et = "duplicate_email"
    else:
        et = "human_verification"
    return {
        "ok": False, "bundle": None,
        "error_type": et,
        "error_detail": f"OAuth 阻断: {blocked.reason or et} (step={blocked.step})",
        "retryable": False,   # 单次失败,不要白跑 attempts
    }
except Exception as exc:
    ...
```

`retryable=False` 让 `_login_codex_with_result` 的外层 attempt loop 立刻 break(`manager.py:1199` 已经在 `not retryable` 时 return)。

### Fix B(必做)— retry_after 全局 ceiling

`_auth_repair_retry_delays` 与 `_auth_repair_add_phone_retry_delays` 共同提供一个 `_clamp_retry_delay(secs)` helper:

```python
_AUTH_RETRY_DELAY_MAX_SECONDS = 24 * 3600  # 24h hard cap

def _clamp_retry_delay(seconds: int) -> int:
    return max(60, min(int(seconds), _AUTH_RETRY_DELAY_MAX_SECONDS))
```

把 `retry_delays = (interval*2, interval*4, interval*6)` 改为
`tuple(_clamp_retry_delay(interval*m) for m in (2,4,6))`;
add_phone 同理 `tuple(_clamp_retry_delay(interval*(2**i)) for i in range(retries))`。

24h 是合理上限:超过 24h 的"自动修复冷却"等价于"放弃",运维直接看 paused 状态。

### Fix C(防御性)— interval 入口防爆

`api.py:4430` 给 `_auto_check_config["interval"]` 加 ceiling:

```python
_AUTO_CHECK_INTERVAL_MAX = 24 * 3600
_auto_check_config["interval"] = max(60, min(_AUTO_CHECK_INTERVAL_MAX, cfg.interval))
```

避免 UI / API 端写入超大值污染 retry_after。

### Fix D(可选)— `_should_aggressively_release_auth_failure` 默认行为

当前需要 `ROTATE_SKIP_REUSE=True`(默认)才会让 "exception" 落进激进 release 分支。
用户(根据反证)关掉了 `ROTATE_SKIP_REUSE`,导致 "exception" 走 else 衰退式。
建议:把 "exception" 从 `AUTH_REPAIR_AGGRESSIVE_RELEASE_TYPES`(`manager.py:268-288`)拿出来,
让它**始终**走 else 衰退式或者 hard-failure 分支,而不是依赖 ROTATE_SKIP_REUSE 开关。
但这会改变其他场景行为,需评估影响面。

---

## 边界测试点

1. **add-phone 单次失败**(Fix A 后):
   - 期望:`error_type="add_phone"`,`_login_codex_with_result` 立即 break(retryable=False),
     `_record_auth_repair_failure` 走 add_phone 分支,delay=interval*2 (clamped ≤24h)。
2. **add-phone 连续 3 次**(同一账号):
   - 期望:retry_count 累加到 max_retries 后,paused=True + release_team_seat=True。
3. **interval = 999999s**(故意脏):
   - 期望:retry_delays max = 24h(clamped),而不是 6,000,000s。
4. **error_type = "exception" + ROTATE_SKIP_REUSE=False**(回归当前 bug):
   - 期望:走 else 衰退式但 delay clamped ≤24h;UI 显示最多 "约 1440 分钟后重试"。
5. **error_type = "exception" + ROTATE_SKIP_REUSE=True**(默认):
   - 期望:aggressive release 分支,paused=True + 释放席位。
6. **RegisterBlocked.is_duplicate=True**(Fix A 兼顾):
   - 期望:error_type="duplicate_email" (新),走 hard-failure 或 single-attempt 分支(需新增到 set)。

---

## 不确定项 / 需进一步 verify

- ⚠️ **361,018 min 的精确来源**:静态分析卡在 "interval=3,600,180s 才数学干净"。若实际是 `_auto_check_config["interval"]` 被污染,需要 dump runtime 状态;若是 `auth_retry_count` 因为 add_phone 分支 max_retries 调大被累加,需要确认环境变量 `AUTO_CHECK_ADD_PHONE_MAX_RETRIES` 是否被设。
- ⚠️ **`_record_auth_repair_failure` 是否在 21:09 真的调用过**:从代码看,add-account 失败路径(`record_failure` + HTTP 409)未必走 `_record_auth_repair_failure`。若没走,21:44 是首次写 retry_after,prev_count=0 → next_count=1 → delay=interval*2(若 interval 干净,=3600s=60min,**不可能 360018**)。这进一步指向 "interval 被污染"。
- 若以上猜想都不对,需要进一步在测试环境复现 21:44 的 stack,加 logging 把 `interval` / `retry_delays` / `next_count` / `delay` 全部 dump。

---

## 引用

- `src/autoteam/manager.py:747-763` — `_auth_repair_retry_delays`
- `src/autoteam/manager.py:784-820` — `_auth_repair_add_phone_*`
- `src/autoteam/manager.py:860-869` — `_auth_repair_state_suffix`
- `src/autoteam/manager.py:940-947` — `_should_aggressively_release_auth_failure`
- `src/autoteam/manager.py:950-1103` — `_record_auth_repair_failure`
- `src/autoteam/manager.py:1150-1226` — `_login_codex_with_result` + `_call_login`(BUG#1 现场)
- `src/autoteam/invite.py:156-163` — `assert_not_blocked` 抛 `RegisterBlocked`
- `src/autoteam/api.py:4427-4441` — `set_auto_check_config`(BUG#2 入口防御点)
- `src/autoteam/config.py:77,113-114,117` — interval / add_phone retries / ROTATE_SKIP_REUSE 默认
- `.upstream/manager.py:208-435` — upstream 同源代码(同样缺 ceiling)
- `D:/Desktop/autoteam-1/AutoTeam/src/autoteam/manager.py:1067,1110,1315-1453` — 旧快照,同结构无回归
- `resource/日志(1).md:701,747,753,779` — 触发流水关键日志行
