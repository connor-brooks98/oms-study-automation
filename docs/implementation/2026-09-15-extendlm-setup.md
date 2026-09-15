# ExtendLM Hub integration — 2026-09-15

Build base: live NUC `0a864d326b8d1e1577c0f653d459eb736dcfd01b`, schema 42.
The September 8 audit deferred ExtendLM; Connor explicitly requested implementation today.

## Implementation

Add an optional NotebookLM upload page linked from Settings, Uploads, and each lecture. Sign in through ExtendLM public-client OAuth with S256 PKCE and a browser-bound, expiring state. Keep grants encrypted on the Hub, scoped to the authenticated Hub owner and browser session. Each computer can sign in independently. Google login stays in that computer's supported browser profile; the Hub never receives Google cookies or passwords.

Discover the connected extensions and Google account indexes, including index `0`; require explicit selection before listing notebooks or uploading. Send PDFs and transcripts through the documented MCP prepare → raw HTTPS PUT → add-source flow. Use the Hub's existing validated lecture PDF and cleaned-transcript artifacts for saved lectures. Keep durable per-file receipts and stable idempotency keys. Background uploads continue after leaving the page while the extension browser remains open. Interrupted jobs require an explicit resume; never silently reroute them to another browser/account. Accepted sources remain distinct from indexed/ready sources.

Reuse httpx, existing encrypted storage, CSRF, Cloudflare Access, artifact validation, and UI styles. No new dependencies or changes to the core GPT study path. PPTX files go through the existing Hub ingestion/conversion flow before sending the resulting PDF.

## Verified vendor contracts

- [MCP setup](https://extendlm.com/support/mcp): supported extension browser must remain running, signed into both ExtendLM and Gemini Notebook in the same profile, MCP enabled and connection Connected. Public PKCE clients; no client secret. Sign-in and permission approval take place on ExtendLM.
- [Import sources](https://extendlm.com/support/import-sources): local folder and PDF imports are supported.
- [Tool catalog](https://mcp.extendlm.com/.well-known/extendlm-tools): version 2.5.1, 149 tools observed. Use `list_notebook_users`, `get_notebooklm_capabilities`, `list_notebooks`, `list_notebook_sources`, `prepare_pdf_source_upload`, `prepare_file_source_upload`, `add_pdf_source`, and `add_file_source`. Source writes require `notebooks:read sources:read sources:write exports:create`; PDF source acceptance is not indexing completion. PDF uploads explicitly use `split_mode=single` to preserve lecture source identity.
- [Protected resource](https://mcp.extendlm.com/.well-known/oauth-protected-resource): resource `https://mcp.extendlm.com/mcp`, issuer `https://api.extendlm.com`.
- [OAuth metadata](https://api.extendlm.com/.well-known/oauth-authorization-server): public dynamic registration, authorization code, refresh tokens, S256, issuer response parameter.
- [HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports): JSON/SSE, initialization, negotiated protocol and session headers.

## Sign in on another computer

1. Open the private Study Hub URL and complete its existing Cloudflare Access sign-in.
2. Install [ExtendLM from Chrome Web Store](https://chromewebstore.google.com/detail/extendlm-notebooklm-exten/jefclkefiknlccjcjmkkhlcfkdgcmgcm) in a supported desktop browser. Sign into ExtendLM and [Gemini Notebook](https://notebook.google.com/) in that profile.
3. In ExtendLM: Manage Notebooks → profile → Settings → MCP → Enable MCP. Confirm Connected.
4. In Hub Settings → ExtendLM uploads, choose Sign in. Review the requested upload permissions on ExtendLM's page.
5. Refresh connections, select the intended browser/account and destination notebook, then send a saved lecture or choose local PDFs/transcripts.

The OAuth callback uses the configured private HTTPS Hub hostname, or the exact loopback URL for local development. No public callback bypass is needed; it remains behind existing Hub authentication. The in-app browser cannot host the Chrome extension. A mobile or managed browser without extension support cannot provide its own bridge.

## Acceptance

Implementation and mock tests do not establish live provider acceptance. Completion requires deploying the reviewed code, signing in, seeing the actual extension/account/notebooks, and verifying source IDs and indexing state for a chosen upload. No account authorization, production upload, or deployment is claimed by this document.

### Local validation

- 12 new contract/security tests pass, including PDF and transcript byte uploads, encrypted OAuth grants, PKCE, callback replay prevention, owner/browser isolation, account `0`, JSON/SSE responses, refresh/revocation, pinned upload destinations, duplicate reuse, source indexing state, and interrupted-add recovery across restart without re-uploading.
- 64 surrounding Python checks passed (Settings, daily study UI, Cloudflare Access, lecture material controls, and existing NotebookLM upload behavior).
- 54 existing Settings/upload JavaScript tests pass; new script parses; the page was inspected in the browser and rendered screenshot.
- Ruff and focused strict mypy pass.
- One existing test fails on both this branch and a clean archive of `0a864d3`: `tests/study_generation/test_notebook_upload_only.py::test_optional_queue_failure_does_not_fail_completed_gpt_ingestion`. Its fake ingestion repository lacks `database` and `fail_job` required by the existing process-control wrapper. No production ingestion code was changed for that failure.

Operational limits: grants and history are per browser sign-in, expire locally after 30 days, and stay in encrypted Hub storage. Reauthorizing creates a separate history; previously accepted sources must be reviewed in NotebookLM before deliberately starting equivalent work under a new grant. Explicit disconnect revokes the grant. Receipts preserve the exact original browser/account; reconnecting a different bridge will not silently reroute a queued job. This release supports existing destination notebooks; create new notebooks in NotebookLM first.
