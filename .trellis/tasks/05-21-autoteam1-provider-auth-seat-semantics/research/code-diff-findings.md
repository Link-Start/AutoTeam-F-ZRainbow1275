# Code Diff Findings

## Scope

对比对象：

* Current repo: `D:\Desktop\AutoTeam`
* Source repo: `D:\Desktop\autoteam-1\AutoTeam`

本次只记录与用户点名范围相关的 backend runtime 语义：CPA/CLIProxy provider-auth 低水位、pending invite 席位占用、以及 auto-check 对昂贵探测的约束。

## Git facts

* Current latest: `be34853 fix: reduce rotation probe interference`
* Source latest: `2f8d52d fix:reduce-rotation-probe-interference`
* Source provider-auth gate baseline: `8f17448 fix: add CPA credential gate to auto-check`
* Source verified-domain/direct-first baseline: `c93d246 fix: prefer domain auto-join for rotation account creation`

## Findings

### 1. Current has busy-skip / fast status, but not low-water provider-auth

Current `src/autoteam/api.py` already includes:

* `_safe_cliproxy_health(**kwargs)` with bounded fast status usage.
* `GET /api/status?fast=true` tests.
* task `progress_history`.
* auto-check skip when `_playwright_lock` / current task indicates a busy Playwright-bound operation.

But current auto-check still treats CPA gate mostly as zero-credential only:

* `_collect_cpa_credential_gate()` returns `zero_available`.
* auto-fill branches check `cpa_credential_gate.get("zero_available")`.
* There is no `_cpa_provider_auth_below_pool_target()` helper in current `api.py`.
* Current `zero_available` expression is `management_ok and available <= 0`; source includes `safe_read_only and management_ok and available <= 0`.

Source behavior adds:

* `_cpa_provider_auth_below_pool_target(gate, pool_target)`.
* Auto-check `provider_auth_below_target` when `available < pool_active_target`.
* Preventive rotate when Team is full and provider auth is below target, even when `available` is not zero.
* Runtime validation degraded follow-up when provider-auth available is below pool target.

### 2. Pending invite is counted in manager capacity but not current API auto-check

Current `manager.py` already has `_remote_team_occupancy()` and `_has_remote_capacity_for_new_seat()`, and logs `members`, `invites`, `total`. This protects the create path from blindly adding a new seat when pending invite occupies capacity.

Current `api.py` auto-check still has `_auto_check_team_member_count()` returning plain `int(result["count"])` from the Playwright probe. It does not carry `invites` or `occupancy` into:

* Team-full decision.
* Over-cap cleanup trigger.
* cleanup task params.

Source behavior adds:

* `_TeamMemberCount(int)` with `.invites` and `.occupancy`.
* `_team_member_invite_count()` and `_team_member_occupancy()`.
* `_auto_check_team_member_count()` parses `count`, `invites`, `occupancy` from probe JSON.
* auto-check uses `actual_team_occupancy > target_seats` for cleanup and includes `invite_count` / `team_occupancy` params.
* Unit test `test_auto_check_triggers_cleanup_when_pending_invite_exceeds_occupancy`.

### 3. Direct file replacement would be unsafe

The current repo has local divergence that source `autoteam-1` does not fully share:

* Round 11 OAuth/backoff logic.
* Current status/task APIs and single-main operating contract.
* Current setup diagnostics and frontend docs.
* Existing pending-invite capacity logic in `manager.py`.

Therefore the implementation should adapt the source semantics into the current structure instead of replacing `api.py`.

## Recommended MVP

Implement a narrow semantic patch:

1. Add source-style provider-auth below-target helper and safe-read-only guard.
2. Add `_TeamMemberCount` carrier and helper functions to `api.py`.
3. Update current auto-check branches to use provider-auth low-water trigger and pending invite occupancy.
4. Update runtime validation to include provider-auth below-target degraded follow-up.
5. Add unit tests adapted to current repository shape.

## Verification targets

Suggested targeted commands after implementation:

```bash
.\.venv\Scripts\python.exe -m ruff check src/autoteam/api.py tests/unit/test_api_status.py
.\.venv\Scripts\python.exe -m pytest tests/unit/test_api_status.py -q
```

If time allows, add current related suites:

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_manager_rotate.py tests/unit/test_manager_fill.py -q
```
