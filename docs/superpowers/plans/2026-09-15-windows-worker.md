# Windows Worker Implementation Plan

> **For agentic workers:** Execute inline using superpowers:executing-plans. Preserve the running Hub until native acceptance and review pass.

**Goal:** Run the existing subscription-backed quiz worker on Windows with native isolation and persisted managed login.

**Architecture:** Keep the shared Codex session, queue, and quiz pipeline. Add a Windows LPAC process transport using the accepted suspended-launch/token-verification mechanism; permit only the pinned runtime and validated model.

**Tech Stack:** Python stdlib ctypes, Win32 AppContainer APIs, existing Codex app-server and pytest.

**Spec:** `docs/implementation/gpt-platform-handoffs/O-runtime-acceptance-20260914.md`, plus Connor's 2026-09-15 Windows implementation instruction.

## Global constraints

- Keep subscription login; no API billing fallback or Linux implementation.
- Native file and child-process restrictions must hold before a provider turn.
- Dedicated persistent login home; request images read-only; no ambient configuration or credentials.
- Preserve live source, database, scheduled task, and processes until reviewed deployment.

### Task 1: Native transport

Files: create `src/oms_hub/llm/windows_lpac.py`; modify `src/oms_hub/llm/codex_session.py`; test `tests/llm/test_codex_windows_session.py`.

- [x] Port the accepted SECURITY_CAPABILITIES, LPAC opt-out, handle list, and child-process policy to ctypes. Launch suspended and verify caller/child tokens before ResumeThread.
- [x] Use the existing bounded `_Stdio` readers/writer and cancellation. Hold an owned process handle; close it only after termination is confirmed. Use a kill-on-close job for parent failure.
- [x] Grant the profile access only to private home, current request directory, and pinned executable. Preserve auth across restarts and deny config.toml.
- [x] Run native staged-read/private-write, outside-read/write, descendant denial, interruption, and cleanup checks using this launcher.

### Task 2: Shared generation readiness

- [x] Replace the obsolete Windows binary pin with the accepted private binary. Keep Mac acceptance unchanged.
- [x] Require the selected Windows model to be both accepted and advertised with the requested modalities. Verify managed account, rate limits, policy echo, and exact runtime home/version.
- [x] Run `uv run pytest tests/llm/test_codex_session.py tests/llm/test_codex_tool_policy.py tests/llm/test_codex_macos_session.py tests/llm/test_codex_windows_session.py`.
- [x] Prove native synthetic text/image/schema output and persisted account using the candidate runtime before enabling production.

### Task 3: Release and workflow

- [x] Obtain fresh read-only review of the security-sensitive diff and acceptance evidence.
- [x] Back up deployment/database; stage exact source and runtime hashes; deploy with rollback and health checks.
- [x] Queue a new GPT-5.5 quiz after confirming the earlier immutable Terra run has no provider attempt; inspect generated review artifact and expected export verification gates (HTTP 409, no publication).
- [x] Record deployment/native evidence and defer Linux migration in the future ideas document.
- [x] Record final real-lecture result: 24 questions reached review, 25/25 coverage groups, 12 images; healthy throughout, answers remain unverified. See `O-windows-worker-20260915.md` for evidence and limits.
