# Apply autoteam-1 hardening and Docker deployment features

## Goal

Apply selected stability, overflow-prevention, and Docker deployment hardening from `D:\Desktop\autoteam-1` to the current `D:\Desktop\AutoTeam` project, while preserving the current free-account registration flow.

## Authoritative Planning Docs

- `prompts/0515/prd.md`
- `prompts/0515/spec.md`
- `prompts/0515/implementation-plan.md`
- `prompts/0515/research-docker-playwright-hardening.md`

## What I Already Know

- Current AutoTeam already contains newer Round 11/12 free registration protections: `leave_workspace=True`, Personal OAuth retry, plan drift recording, workspace pool, register dual path, master health checks, and related tests.
- `D:\Desktop\autoteam-1\AutoTeam` contributes useful low-coupling hardening: Docker resource boundaries, `runtime_resources.py`, `playwright_lifecycle.py`, `Dockerfile.fast`, `chatgpt_transport.py`, and SignupProfile propagation patterns.
- `D:\Desktop\autoteam-1\codex-watchdog` history warns that automation safety must be conservative and dry-run verifiable.

## Requirements

- Preserve current free-account registration semantics and tests.
- Add Docker/Compose hardening without removing existing build fingerprint and entrypoint self-check contracts.
- Add runtime resource probes and Playwright lifecycle cleanup with unit coverage.
- Keep `curl_cffi` isolated to backend Team API calls; after explicit second-round alignment, default transport is `auto` to match the mother template while OAuth/UI flows force browser context.
- Preserve a single `SignupProfile` snapshot through registration and post-register Codex OAuth.
- Make that `SignupProfile` snapshot truly immutable at nested birthday-field level, not only frozen at the dataclass attribute level.
- Make OAuth about-you fail fast and retry supported birthday field orders instead of silently continuing when the page remains on about-you.
- Ensure status/resource and browser fallback failures are contained at their API/lifecycle boundaries.
- Ensure registration/OAuth Playwright call sites no longer rely on raw close calls and that direct registration cleanup also runs for unexpected navigation/page errors.
- Ensure HTTP transport fallback closes failed transports before using browser fallback, including request exceptions and retry-after-refresh exceptions.

## Acceptance Criteria

- [ ] `prompts/0515` docs are present and accepted before implementation.
- [ ] Docker compose has init, shm, memory/PID boundaries, resource env vars, and healthcheck.
- [ ] Runtime resource probe gracefully handles missing cgroup/proc files.
- [ ] Playwright cleanup is idempotent and cleans partially initialized browser sessions.
- [ ] Free registration regression tests pass.
- [ ] Registration and OAuth about-you use the same `SignupProfile` snapshot; no hardcoded OAuth `User / 1995-06-15 / 25` fallback remains on the protected path.
- [ ] `SignupProfile.birthday` cannot be mutated after construction, and constructor input mutation cannot alter the profile.
- [ ] OAuth about-you retries supported birthday field orders and returns failure if the profile page never exits.
- [ ] `ChatGPTTeamAPI.start_with_session()` cleans up partially initialized browser resources when browser fallback fails.
- [ ] Registration/OAuth paths use `close_playwright_objects()` instead of raw Playwright close calls, with direct registration guarded by `try/finally`.
- [ ] HTTP transport request/retry exceptions close the bad transport and fall back to browser.
- [ ] `/api/status` keeps returning a response if runtime resource snapshot collection unexpectedly fails.
- [ ] Docker guard and relevant unit tests pass.

## Out of Scope

- Rewriting free registration.
- Directly overwriting current manager/codex OAuth code with older `autoteam-1` files.
- Letting HTTP transport handle free registration, Personal OAuth, captcha, or workspace UI flows.

## Research References

- `research/docker-playwright-hardening.md`

## Technical Approach

Use a staged implementation:

1. Docker/Compose hardening.
2. Runtime resource and Playwright lifecycle hardening.
3. Free registration regression verification.
4. Team API transport evaluation as a separate later stage.
5. SignupProfile propagation audit after transport alignment.

## Definition of Done

- Tests added/updated where behavior changes.
- Lint/static checks pass.
- Docker config validates.
- No unverified claim of Docker runtime success without a real command result.
- Docs updated with rollout and rollback notes.
