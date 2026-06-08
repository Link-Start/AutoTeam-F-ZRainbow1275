# 0515 实施计划：先加固外围，再保护主流程

## 当前阶段

状态：已实施并进入深度收尾审计。Phase 1-4 先按保守方式落地，第二轮已按母板把 Team API transport 默认值校正为 `auto`，并补齐 OAuth/UI 浏览器隔离；第三轮继续对照母板后补齐 `SignupProfile` 从注册到 Codex OAuth 的贯穿；第四轮深查继续修复嵌套不可变、OAuth about-you 反馈闭环、browser fallback cleanup 和 `/api/status` 资源快照边界；第五轮继续补齐注册/OAuth 裸 close、SessionCodexAuthFlow 启动失败清理、HTTP transport 异常回退清理。

## TODO

- [x] 创建 Trellis planning task：`.trellis/tasks/05-15-autoteam1-hardening-docker-apply`
- [x] 建立 `prompts/0515` 文档目录
- [x] 写入 PRD、SPEC、实施计划、调研记录
- [x] 用户确认实施范围
- [x] Phase 1：Docker/compose 加固
- [x] Phase 2：runtime resources 与 Playwright lifecycle
- [x] Phase 2.5：killable Playwright probe 吸收
- [x] Phase 3：free 注册保护回归
- [x] Phase 4：Team API transport 评估与母板对齐实施
- [x] Phase 5：完整验证与收尾记录
- [x] Phase 6：完成审计，确认 autoteam-1 优秀设计已吸收或被当前更强实现替代
- [x] Phase 7：深查后修复 SignupProfile 未透传到 OAuth about-you 的隐藏不符合点
- [x] Phase 8：再深查后修复 profile 嵌套可变、OAuth about-you 伪成功、browser fallback 泄漏和 status 快照边界
- [x] Phase 9：继续排查 Playwright 裸 close、SessionCodexAuthFlow 和 HTTP transport retry 异常回退

## 推荐实施顺序

### Phase 1：Docker/compose 加固

变更范围：

- `docker-compose.yml`
- `Dockerfile.fast`
- `docs/docker.md`
- `tests/integration/test_docker_guard.py`

工作内容：

- 给 compose 增加 `init: true`、`shm_size: "1gb"`、`mem_limit`、`memswap_limit`、`pids_limit`。
- 增加资源阈值环境变量。
- 增加 `/api/version` healthcheck。
- 新增 `Dockerfile.fast`，但保留当前 Dockerfile 的版本指纹和 entrypoint self-check 约束。
- 扩展 Docker guard 测试覆盖上述静态契约。

验证：

- `python -m pytest tests/integration/test_docker_guard.py`
- `docker compose config`

### Phase 2：runtime resources 与 Playwright lifecycle

变更范围：

- `src/autoteam/runtime_resources.py`
- `src/autoteam/playwright_lifecycle.py`
- `src/autoteam/chatgpt_api.py`
- `src/autoteam/api.py`
- `tests/unit/test_runtime_resources.py`
- `tests/unit/test_api_playwright_cleanup.py`

工作内容：

- 新增资源探针，支持 cgroup memory/pids 和 browser zombie 统计。
- 在低风险 API status 或自动巡检日志中接入资源快照。
- 新增统一 Playwright close helper。
- 修改 `ChatGPTTeamAPI._launch_browser()` 和 `stop()` 以保证失败清理和幂等释放。
- 对 API 中创建 `ChatGPTTeamAPI` 的失败路径补清理。

验证：

- `python -m pytest tests/unit/test_runtime_resources.py`
- `python -m pytest tests/unit/test_api_playwright_cleanup.py`
- `python -m pytest tests/unit/test_playwright_guard.py tests/static/test_playwright_hygiene.py`

### Phase 3：free 注册保护回归

变更范围：

- 原则上不改 free 注册代码，只跑并补充保护测试。

验证：

- `python -m pytest tests/unit/test_round11_personal_oauth_retry.py`
- `python -m pytest tests/unit/test_round11_session_token_injection.py`
- `python -m pytest tests/unit/test_round12_s4_register_dual_path.py`
- `python -m pytest tests/unit/test_manager_fill.py`
- 如 Phase 2 触碰 API task 调度，再补跑 `tests/unit/test_round12_rotate_sse_stream.py`

### Phase 4：Team API transport 评估

已在 Phase 1-3 稳定后实施，并在第二轮对照母板后改为默认 `auto`：

- 新增 `curl-cffi` 依赖和 `chatgpt_transport.py`
- 默认对齐 `autoteam-1` 为 `auto`
- 仅用于非注册、非 OAuth、非 workspace UI 的 backend API fetch
- HTML challenge / token miss / 403 / 401 自动回退 Playwright
- `SessionCodexAuthFlow` 等 OAuth/UI 路径显式 `require_browser=True`

验证：

- 新增 transport 单测
- 确认 free 注册保护测试仍通过

### Phase 6：吸收审计

工作内容：

- 建立 `autoteam-1` 设计点到当前 artifact 的 checklist。
- 对每个显式需求和源端突出设计标记：已吸收 / 当前更强实现替代 / 明确拒绝。
- 用文件、测试、Docker 运行结果和 lint 结果作为证据，不能只用意图或清单。

### Phase 7：SignupProfile 贯穿修复

深查发现：

- 当前文档曾判断 `SignupProfile` 已贯穿注册和 OAuth，但真实 `codex_auth.login_codex_via_browser()` 的 about-you 分支仍填硬编码 `User`、`1995/06/15`、`25`。
- 直接注册路径 `_complete_direct_about_you()` 也在注册页临时生成身份数据，没有把该快照传给 `_run_post_register_oauth()`。

修复结果：

- `codex_auth._complete_oauth_about_you()` 使用传入的 `SignupProfile`。
- `login_codex_via_browser(..., signup_profile=...)`、`_run_post_register_oauth(..., signup_profile=...)` 完成透传。
- `_complete_registration()` 与 `create_account_direct()` 均复用同一份 profile 到 OAuth。
- `generate_signup_profile(today=..., rng=...)` 现在对姓名和生日都可预测，便于回归测试。

验证：

- `python -m pytest tests/unit/test_free_registration_hardening.py tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_round11_session_token_injection.py`
- `python -m ruff check src/autoteam/identity.py src/autoteam/signup_profile.py src/autoteam/invite.py src/autoteam/codex_auth.py src/autoteam/manager.py tests/unit/test_free_registration_hardening.py tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_round11_session_token_injection.py`
- `python -m pytest tests/unit` -> `740 passed, 1 warning`
- `python -m ruff check src tests` -> `All checks passed`

### Phase 8：再深查加固

深查发现：

- `SignupProfile` 虽是 frozen dataclass，但 `birthday` 是普通 `dict`，外部仍可执行 `profile.birthday["year"] = "1900"`，违反“single immutable snapshot”。
- OAuth about-you 虽已使用 profile，但只尝试第一种生日顺序；如果页面仍停留在 about-you，也会继续进入 consent loop，导致失败被伪装成后续 OAuth 超时或 bundle missing。
- `ChatGPTTeamAPI.start_with_session()` 只覆盖 `_launch_browser()` 内部半初始化失败；如果浏览器已建好后在导航、token fetch、workspace detect 失败，仍可能留下 browser/context/page。
- `/api/status` 直接调用 `collect_runtime_resource_snapshot()`，没有 API 边界兜底，不满足“资源采集不得让 status 失败”的 spec。

修复结果：

- `SignupProfile.__post_init__()` 将 `birthday` 防御性拷贝为只读、hashable 的内部映射，保持 `profile.birthday["year"]` 读法和 `dict(profile.birthday)` 兼容。
- OAuth about-you 使用同一 profile 的三种生日顺序重试；全部失败时返回 `False`，`login_codex_via_browser()` 返回 `None` 交给既有 personal retry / Team failure 分类。
- browser fallback 路径失败时调用 `stop()` 清理 page/context/browser/playwright 并重抛原异常。
- `/api/status` 改用 `_safe_runtime_resource_snapshot()`，资源采集异常时仍返回 status 响应并携带诊断字段。

验证：

- `python -m pytest tests/unit/test_free_registration_hardening.py tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_api_playwright_cleanup.py tests/unit/test_chatgpt_transport.py tests/unit/test_api_status.py tests/unit/test_runtime_resources.py` -> `75 passed, 1 warning`
- `python -m pytest tests/unit/test_chatgpt_transport.py tests/unit/test_round11_api_fetch_header_sanitize.py tests/unit/test_api_playwright_cleanup.py tests/integration/test_docker_guard.py tests/unit/test_runtime_resources.py tests/unit/test_free_registration_hardening.py tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_round11_session_token_injection.py tests/unit/test_round11_personal_oauth_retry.py tests/unit/test_round12_s4_register_dual_path.py tests/unit/test_manager_fill.py` -> `146 passed, 1 warning`
- `python -m pytest tests/unit` -> `746 passed, 1 warning`
- `python -m ruff check src tests` -> `All checks passed`

## 推荐选项

实际执行结果：Phase 1-3 先完成，之后补做 Phase 4；第二轮 codex review 后以 `D:\Desktop\autoteam-1\AutoTeam` 为规范，将默认值校正为 `auto`。

- Docker 和资源清理收益高，风险低，已完成。
- free 注册主流程未改变关键语义；本轮仅修复身份快照贯穿，已通过回归保护。
- 第四轮加固没有改变 free 注册成功路径；只把不一致/伪成功/资源泄漏路径提前失败或兜底。
- `curl_cffi` transport 已按母板默认 `auto` 落地，并通过 `require_browser=True` 与生命周期清理保护真实浏览器路径。

### Phase 9：继续排查修复

深查发现：

- 当前项目虽已有 `close_playwright_objects()`，但 `manager._complete_registration()`、`manager._register_direct_once()`、`invite.run()`、`codex_auth.login_codex_via_browser()` 仍残留裸 `browser.close()` 或临时页 `page.close()`，与母板统一 cleanup 设计不一致。
- `_register_direct_once()` 只在已知 early-return 点 cleanup；如果 `page.goto()` 等未知异常抛出，会绕过 cleanup。
- `SessionCodexAuthFlow.start()` 在 `ChatGPTTeamAPI.start_with_session(..., require_browser=True)` 成功后，如果 `context.new_page()` / cookie 注入 / `page.goto()` / `_advance()` 失败，会留下已启动的 browser session。
- `_direct_api_fetch()` 首次 `http_transport.request()` 抛异常，或 401 refresh 后的 retry request 抛异常时，没有关闭坏 transport 并回退浏览器。
- HTML/challenge fallback 后如果保留坏 `http_transport`，后续 `_api_fetch()` 会继续优先撞坏 transport。

修复结果：

- `manager._complete_registration()` 和 `invite.run()` 改为 `try/finally` 调统一 cleanup。
- `_register_direct_once()` 用本地 `cleanup_direct_register()` 加 `try/finally` 包住直接注册全流程；成功路径仍先抽 session token，再 cleanup，再返回 `(success, session_token)`。
- `codex_auth.login_codex_via_browser()` 和相关临时页关闭改为 `close_playwright_objects()`。
- `SessionCodexAuthFlow.start()` 对启动阶段异常调用 `self.stop()`。
- `ChatGPTTeamAPI._direct_api_fetch()` 对首次 request 和 refresh 后 retry request 异常均关闭/清空 `http_transport` 后回退浏览器；HTML/challenge/auth fallback 也清空坏 transport。

验证：

- `python -m pytest tests/unit/test_api_playwright_cleanup.py tests/unit/test_round11_session_token_injection.py` -> `29 passed, 1 warning`
- `python -m pytest tests/unit/test_api_playwright_cleanup.py tests/unit/test_round11_fresh_relogin_fallback.py tests/unit/test_round11_session_token_injection.py` -> `43 passed, 1 warning`
- `python -m pytest tests/unit/test_chatgpt_transport.py` -> `10 passed`
- `python -m pytest tests/unit/test_chatgpt_transport.py tests/unit/test_api_playwright_cleanup.py tests/unit/test_round9_retroactive_helper.py tests/unit/test_round12_s3_cherry_pick.py tests/unit/test_manager_reinvite.py tests/unit/test_manager_fill.py tests/unit/test_free_registration_hardening.py tests/unit/test_round11_session_token_injection.py tests/unit/test_round11_personal_oauth_retry.py tests/unit/test_round12_s4_register_dual_path.py` -> `151 passed, 1 warning`
- `python -m ruff check src/autoteam/manager.py src/autoteam/invite.py src/autoteam/codex_auth.py src/autoteam/chatgpt_api.py tests/unit/test_api_playwright_cleanup.py tests/unit/test_round11_session_token_injection.py tests/unit/test_chatgpt_transport.py` -> `All checks passed`
