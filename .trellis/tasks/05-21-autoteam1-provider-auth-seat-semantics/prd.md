# brainstorm: align autoteam-1 latest provider-auth seat semantics

## Goal

把当前 `D:\Desktop\AutoTeam` 继续对齐 `D:\Desktop\autoteam-1\AutoTeam` 最新一轮轮转/巡检语义，重点补齐 CPA/CLIProxy provider-auth 低水位提前触发，以及 pending invite 也计入远端 Team 席位占用，避免本地账号看似健康但远端凭证池或邀请占位已经阻塞后续轮转。

## What I already know

* 用户明确点名需要继续对齐 `D:\Desktop\autoteam-1` 最新改动，例子包括 CPA/CLIProxy provider-auth 低水位提前触发、pending invite 也算远端席位占用。
* 当前工作区是 `D:\Desktop\AutoTeam`，Trellis 当前无 active task，因此已新建本任务记录这轮对齐。
* 当前仓库最新提交是 `be34853 fix: reduce rotation probe interference`；源仓库 `D:\Desktop\autoteam-1\AutoTeam` 最新提交是 `2f8d52d fix:reduce-rotation-probe-interference`。
* 当前仓库已吸收 `GET /api/status?fast=true`、任务 `progress_history`、auto-check 忙碌时跳过昂贵探测等部分内容。
* 当前仓库 `src/autoteam/api.py` 仍主要只用 `zero_available` 触发 CPA gate，缺少源仓库的 `_cpa_provider_auth_below_pool_target()` 低水位判断。
* 当前仓库 `_collect_cpa_credential_gate()` 的 `zero_available` 判断未完全对齐源仓库的 `safe_read_only and management_ok and available <= 0` 安全边界。
* 当前仓库 `_auto_check_team_member_count()` 只返回成员数 `count`；源仓库新增 `_TeamMemberCount`，保留 int 兼容同时携带 `invites` 与 `occupancy`。
* 当前仓库 `manager.py` 已在创建新账号前使用 `members + invites` 判断远端容量，但 `api.py` 的 auto-check 决策层尚未把 pending invite 计入远端占用。
* 当前仓库有本地状态机、OAuth backoff、setup diagnostics、前端等扩展，不能整文件覆盖源仓库 `api.py`，需要做语义级小补丁。

## Assumptions (temporary)

* 本轮目标是继续把当前仓库补齐到 `autoteam-1` 的运行语义，而不是重写当前仓库已有的 Round 11 状态机、状态 API 或单 main 主线。
* 本轮保持单 main 主线：Team 目标仍按 `1 owner/main + 2 managed children = 3 seats` 计算，provider-auth 池目标只对应 managed children（`target_seats - 1`），不迁移或启用多 owner/multi-master 调度。
* provider-auth 低水位应按池目标判断：目标子号数为 2 时，只要 CLIProxy provider-auth `available < 2` 且检查是 read-only、management OK，就应触发预防性轮转/同步，而不是等到 `available == 0`。
* pending invite 计入远端占用的主战场是 auto-check 与自动 cleanup/rotate 的判断参数；前端 `/api/team/members` 已能展示 invite，不作为本轮主要 UI 改造。

## Open Questions

* None.

## Requirements (evolving)

* MVP 采用语义小补丁：只补 provider-auth 低水位、safe_read_only guard、pending invite 占用和对应测试，不做 auto-check 大重构。
* 新增 provider-auth 低水位 helper，且仅在 CPA sync 已启用、CLIProxy health 是 read-only、management API OK 时生效。
* `zero_available` 的安全条件必须包含 `safe_read_only`，不能把未知/失败的 CLIProxy 管理检查当作 0 可用凭证。
* auto-check 应在 provider-auth `available < pool_active_target` 时提前触发正常 `cmd_rotate(..., background_post_sync=True)` 路径，并在 task params 中暴露 `provider_auth_below_target`、`provider_auth_available`、`provider_auth_target`。
* post-task runtime validation 应在 provider-auth 低于池目标时标记 degraded follow-up，而不是仅检查本地 auth file 与 live quota。
* Team 成员探针应兼容返回 `count`、`invites`、`occupancy`，并保持旧调用方把返回值当 `int` 使用不破坏。
* auto-check 清理判断必须使用远端占用 `members + invites`，当占用超过目标时触发 cleanup，并传入 `invite_count` / `team_occupancy` 诊断参数。
* pending invite 不等同于可删除证据；只能清理已能证明是 AutoTeam 遗留的 stale pending invite，不能删除未知来源邀请。

## Acceptance Criteria (evolving)

* [x] `tests/unit/test_api_status.py` 覆盖 provider-auth `available=1,target=2` 时触发提前 rotate/sync。
* [x] `tests/unit/test_api_status.py` 覆盖 runtime validation 在 provider-auth 低水位时返回 degraded follow-up。
* [x] `tests/unit/test_api_status.py` 覆盖 CLIProxy management failure / non-read-only 时不触发低水位轮转。
* [x] `tests/unit/test_api_status.py` 覆盖 Team probe 返回 `count=3, invites=1, occupancy=4, target=3` 时触发 auto-cleanup，并记录 `invite_count=1`、`team_occupancy=4`。
* [x] 现有 auto-check busy skip、fast status、CPA gate zero-available、manager pending invite capacity tests 继续通过。
* [x] 针对本轮触及文件运行 ruff 与 targeted pytest。

## Definition of Done (team quality bar)

* Tests added/updated (unit/integration where appropriate)
* Lint / typecheck / CI green for touched backend files
* Docs/notes updated if behavior changes
* Rollout/rollback considered if risky
* 不使用 mock 代替真实语义；测试桩只模拟边界输入，业务判断必须对应真实 runtime contract

## Out of Scope (explicit)

* 不改 Team 席位上限，仍保持单 Team `1 owner/main + 2 managed children = 3 seats`。
* 不做 create-before-remove，不把 pending invite 当作可以随意删除的远端对象。
* 不改前端视觉/布局。
* 不用 `/api/sync` 作为健康验证或 smoke test，因为它会 mutate 远端 CPA 状态。
* 不整文件覆盖 `src/autoteam/api.py`，避免破坏当前仓库已有 Round 11、OAuth/backoff、状态 API 和单 main 主线。
* 不把源仓库或历史分支里的 multi-master / 多 owner 调度作为本轮对齐目标。

## Technical Notes

* 源仓库最新对齐参考：`D:\Desktop\autoteam-1\AutoTeam` commits `2f8d52d`, `c93d246`, `8f17448`, `276ed0b`, `f120570`。
* 当前仓库参考提交：`be34853`, `120638a`, `ceeba19`, `e49e827`。
* 重点文件预计为 `src/autoteam/api.py`, `tests/unit/test_api_status.py`, 可能涉及 `docs/api.md` 或 backend spec。
* 相关规范：`.trellis/spec/backend/runtime-docker-hardening.md`, `.trellis/spec/backend/account-disable-cpa-sync.md`。
* 本轮代码调研见 `research/code-diff-findings.md`。

## Decision (ADR-lite)

**Context**: 当前仓库已经吸收了 `autoteam-1` 的部分最新变更，但 `api.py` 与源仓库结构明显分叉，直接覆盖会破坏现有 Round 11 OAuth/backoff、setup diagnostics、状态 API 和单 main 主线等本地约束。

**Decision**: 采用“语义小补丁”方案。只把源仓库新增运行契约迁入当前结构：provider-auth 低水位提前触发、`safe_read_only` 安全边界、pending invite 计入 Team 远端占用、runtime validation 低水位 degraded follow-up，以及对应单元测试。

**Consequences**: 回归面更小，能快速对齐用户点名缺陷；代价是当前 auto-check 分支仍保持本地形态，未来若继续迁移源仓库统一 state 收集模型，需要另起任务。

## Research References

* [`research/code-diff-findings.md`](research/code-diff-findings.md) — 当前仓库与 `autoteam-1` 最新 provider-auth / pending invite 语义差异。
