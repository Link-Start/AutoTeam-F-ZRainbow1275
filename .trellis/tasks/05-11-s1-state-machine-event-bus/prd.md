# S1: AccountState 状态机 + 转移日志 + 事件总线

## Goal

为 round-12 后续的 cherry-pick（S3）/ 注册修复（S4）/ 多 workspace 池化（S7）/ 前端实时进度（F2+F3）提供统一的「账号状态」入口：把目前散落在 `manager.py` / `api.py` / `master_health.py` 的 17+ 处 `update_account(email, status=...)` 收敛到一个状态机里，写转移日志 + 发布事件总线。

## Background (verified via grep + serena)

### 现状

- `src/autoteam/accounts.py` 定义了 8 个 `STATUS_*` 字符串字面量：
  `pending / active / exhausted / standby / personal / auth_invalid / orphan / degraded_grace`
- `update_account(email, **kwargs)` 是状态唯一写入点（已确认所有调用方）
- 17+ 处直接 `update_account(email, status=STATUS_X, ...)` 调用,无统一日志,无事件
- 没有非法转移保护——例如 `ARCHIVED/personal → EXHAUSTED` 这种语义错误不会被拦截

### 状态枚举对齐（一一对应）

| 父 PRD 命名     | 现有 `STATUS_*`           | 字面量            |
| --------------- | ------------------------- | ----------------- |
| `PENDING`       | `STATUS_PENDING`          | `"pending"`       |
| `AUTH_PENDING`  | `STATUS_AUTH_INVALID`     | `"auth_invalid"`  |
| `ACTIVE`        | `STATUS_ACTIVE`           | `"active"`        |
| `EXHAUSTED`     | `STATUS_EXHAUSTED`        | `"exhausted"`     |
| `STANDBY`       | `STATUS_STANDBY`          | `"standby"`       |
| `ARCHIVED`      | `STATUS_PERSONAL`         | `"personal"`      |
| `ORPHAN` (扩展) | `STATUS_ORPHAN`           | `"orphan"`        |
| `DEGRADED_GRACE`(扩展) | `STATUS_DEGRADED_GRACE` | `"degraded_grace"` |

枚举 `value` 与现有字面量 1:1 对齐 → `accounts.json` 不变,无需 schema 迁移即可热接入。

## Decision

1. **新建** `src/autoteam/account_state.py`：
   - `class AccountState(str, Enum)`：8 个成员（含父 PRD 6 个 + ORPHAN/DEGRADED_GRACE 兼容扩展）
   - `class IllegalTransitionError(ValueError)`
   - `dataclass Transition(email, from_state, to_state, reason, timestamp, extra)`
   - `class StateMachine`:
     - `transition(email, to_state, reason, extra=None, *, from_state=None) -> Transition`
     - `get_legal_transitions(from_state) -> frozenset[AccountState]`
     - `subscribe(callback) / unsubscribe(callback)`（threading.Lock + 列表）
     - `_write_state_log(transition)`：原子追加（read → tmp + new → rename，含 `.bak` 回滚）
     - `set_state_provider(callable)`：`update_account` 用它做 from_state 查询,避免循环 import
   - `default_machine` 单例 + `DEFAULT_LOG_PATH = PROJECT_ROOT / "state_log.jsonl"`
   - `migrate_legacy_status(accounts_iter)` 一次性迁移工具（None status → PENDING + 报告 unknown）

2. **改** `src/autoteam/accounts.py`：
   - `update_account(email, **kwargs)`：若 kwargs 含 `status` 且与当前不同 → 走 `default_machine.transition()`；非状态字段照旧 update
   - `add_account` 新建后 → `default_machine.transition(None → PENDING, reason="add_account")`
   - 公共 API 签名（add/update/find/load/save/delete/get_*）一律不变
   - 模块加载时调用 `default_machine.set_state_provider(_lookup_status)` 注入查询函数

3. **测试** `tests/unit/test_account_state.py`（新建）：
   - 合法路径：PENDING → ACTIVE / ACTIVE → EXHAUSTED / EXHAUSTED → STANDBY / STANDBY → ACTIVE / ACTIVE → ARCHIVED
   - 非法路径：None → STANDBY、ARCHIVED → EXHAUSTED → 抛 `IllegalTransitionError`
   - 事件总线：subscribe / unsubscribe / 多订阅者 / 重复订阅幂等 / 订阅者抛异常不影响其他订阅者 / 并发安全（threading）
   - `state_log.jsonl` 格式：每行一个 JSON、字段完整、ASCII 安全
   - 原子写：tmp 中途崩溃测试（mock os.replace 抛 OSError → 验证 `.bak` 回滚）
   - `migrate_legacy_status`：None → "pending"、未知字面量进 unknown 列表

### 不做（明确边界）

- 不动 `manager.py` / `api.py` / `master_health.py` 的 17+ 处 update_account 调用（S3 cherry-pick 任务内做语义升级）
- 不写 SSE endpoint（F2 任务）
- 不动 mail provider（S2 任务）
- 不持久化订阅者列表（进程重启即清空,符合事件总线语义）
- 不做日志轮转（state_log.jsonl 增长由后续观察决定,本任务先 atomic append 即可）

## Acceptance Criteria

- [ ] `src/autoteam/account_state.py` 创建,含 AccountState 枚举 / IllegalTransitionError / Transition / StateMachine / default_machine / migrate_legacy_status
- [ ] `src/autoteam/accounts.py` `update_account` / `add_account` 走状态机,签名不变
- [ ] `tests/unit/test_account_state.py` 创建,覆盖合法/非法/事件总线/原子写/迁移工具
- [ ] `ruff check src/autoteam/account_state.py src/autoteam/accounts.py tests/unit/test_account_state.py` 全绿
- [ ] `python -m pytest tests/unit/test_account_state.py -v` 全过
- [ ] 现有 `python -m pytest tests/unit/test_accounts.py` 不退化
- [ ] commit 信息 `feat(round-12 S1): account state machine + transition log + event bus`

## Definition of Done

- 单元测试新增（覆盖率 > 90%,关键分支全覆盖）
- ruff lint 全绿（pyflakes + pyupgrade + bugbear）
- 现有 `pytest tests/` 不出现新 failure
- accounts.json 字面量保持兼容（无 schema 变更）
- `state_log.jsonl` 落到 PROJECT_ROOT,生产环境首次写入会自动创建

## Out of Scope

- manager.py / api.py / master_health.py 的 17+ update_account 调用语义升级（S3）
- 状态机 SSE 推送 endpoint（F2）
- 历史 accounts.json 的离线迁移脚本（migration 工具就位即可,实际跑由用户/S3 触发）
- 状态机持久化（`default_machine` 实例本身无状态,所有真状态在 accounts.json）
- mypy strict（项目目前未启用 mypy CI,本任务不引入额外 type-check 负担——但本文件代码尽量带 type hint）

## Technical Notes

- 原子写策略：`shutil.copy2(path, .bak)` → 写 `.tmp`（含历史 + 新行）→ `os.replace(.tmp, path)` → 删 `.bak`；异常时 `os.replace(.bak, path)` 回滚
- 订阅者快照在锁内取,dispatch 在锁外做 → 避免 callback re-subscribe 死锁
- 订阅者异常 logger.exception 但不影响其他订阅者
- `from_state` 查询通过 `set_state_provider` 注入,避免 `account_state ↔ accounts` 循环 import
- 全部 callback 同步执行（事件总线本期 P0,不引入 asyncio）

## Research References

- 父 PRD: `.trellis/tasks/05-11-upstream-align-register-multimail-frontend-refresh/prd.md`
- 父 research: `research/{caveat-verified-2026-05-11.md, frontend-bright-icon.md, mail-providers-2026.md}`
- 后端规范: `.trellis/spec/backend/{quality,error-handling,logging,database}-guidelines.md`（目前为模板,本任务遵循显式错误 + structured logging + 原子文件操作的通用最佳实践）
