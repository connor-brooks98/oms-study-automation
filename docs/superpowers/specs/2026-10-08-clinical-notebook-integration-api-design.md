# Clinical Notebook external source API

Date: 2026-10-08
Status: proposed design, revised from a Study Hub-specific bridge; no implementation claimed.

## Product requirement

Clinical Notebook must expose a supported integration interface through which other applications can import sources and read sources back out. Study Hub is the first client. The interface must also be usable by an unrelated application or a script without requiring Study Hub, a particular owner, a course/exam catalog, a specific AI provider, or a particular network service.

Each person can install Clinical Notebook, retain their own library and provider setup, and authorize their own integrations. This is a reusable single-user application API. It does not require a hosted account service or multi-tenant SaaS system. Keep authorization attached to an installation/owner and an integration so future ownership changes are not blocked by hardcoded global assumptions. This follows Clinical Notebook's existing single-user-first and provider-agnostic product decisions.

## Existing capabilities and missing boundary

The current backend already imports files/text/links, stores original files, reads extracted text, and exposes source processing status. Reuse those domain services and background processing. Current endpoints are application-internal: one broad bearer credential protects the backend, some responses expose local file paths, source reads can update recently viewed state, and notebook scope is optional in broad source listings. These endpoints are not yet a safe, stable contract for independently authorized integrations.

Create a separate versioned external API with public response models, per-integration authorization, bounded/paginated reads, and published behavior. Do not expose or reuse the native runtime's administrative bearer. Existing internal clients retain their current behavior.

## First-version capability boundary

| Capability | Behavior |
|---|---|
| Discover API | API version, supported operations, file types, byte limits, processing modes, and readiness semantics. No secrets or library content. |
| List notebooks | Only notebooks explicitly granted to the connected application; pagination and stable opaque IDs. |
| Import sources | File bytes or UTF-8 text into a granted existing notebook; per-item receipt and an idempotency key. |
| Inspect imports | Poll only the caller's authorized import receipts; distinguish stored, extracting, readable, indexing, ready, and needs attention. |
| List/read sources | Paginated metadata and extracted text for sources within granted notebooks; return content revision, provenance, available locators, extraction warnings, and readable/indexed state. |
| Export originals | Download the actual original file for an authorized source if it exists and the integration has export permission. Text-only sources report that no original file exists. |

Create/import and read/export are the initial external operations. Do not expose general source deletion, arbitrary backend settings, provider credentials, generation, lesson adoption, or token management to integration tokens. Existing files are never overwritten implicitly. When the same external document identity arrives with changed content, return a revision conflict for explicit review in Clinical Notebook; replacement uses the native owner's existing review workflow until a separately scoped external replacement operation is designed. Reusing an external identity must not silently create duplicate sources or edit sources shared with ungranted notebooks.

“Any application” means any client that implements this published API. It does not mean every vendor's data format is understood without conversion. Publish a minimal script example and exercise it against the same interface as Study Hub.

## Connected applications and permissions

Clinical Notebook owns a Connected Apps settings surface. The owner creates a named connection, selects allowed notebooks and capabilities, sees its last use, and can revoke it. Begin with high-entropy integration tokens issued by Clinical Notebook; store token verifiers rather than recoverable server-side token plaintext. Show a new token once and keep client secrets in the client's secure credential store. The native desktop administrative secret stays private to its runtime. Tokens are installation-specific and never ship in app packages or example files.

Separate capabilities: `notebooks:read`, `sources:import`, `sources:read`, and `sources:export`. Import permission does not imply reading existing sources or downloading their originals. Import-only clients can inspect their own import receipt without gaining source-content access. Selecting capabilities never implicitly grants all notebooks. Revocation takes effect on subsequent requests, including a later download or resumed operation; data already received by another application cannot be recalled.

Check current source-to-notebook membership on every detail, text, file, revision, status, and list request. A caller-supplied notebook ID is not authorization. Hide ungranted notebook associations and use non-enumerating errors for inaccessible source IDs. A source shared with an allowed notebook is readable under that grant; do not return other linked notebooks' identities. Enforce grants before pagination so pages and counts do not leak the rest of the library. New external routes remain authenticated even when development configuration disables the old internal password middleware.

Scope import keys and external document identities by installation, integration, and destination notebook. Two applications using the same key cannot collide with each other. Tokens cannot mint tokens, access the administrative API, change their grants, or change processing defaults. Use bounded requests and per-integration limits; authorization errors and logs must not reveal tokens, source text, or local paths.

## Generic source contract

Use a versioned REST contract, with an authoritative OpenAPI artifact owned by the Clinical Notebook repository, proposed path `docs/contracts/source-api-v1.openapi.yaml`. Implement both external routes and the Study Hub client against its fixtures. Do not independently maintain a different Hub-shaped receiver contract.

Core identities are integration, notebook, source, import, external document, and content revision. File descriptors carry original filename, media type, byte count, and SHA-256. Text imports carry UTF-8 text and its checksum. Responses use opaque IDs and authenticated API routes; never expose filesystem paths, provider secrets, or arbitrary download URLs. Preserve the original bytes when an original exists. Include extraction provenance and page/slide/paragraph/timestamp locators only where actually available; missing locators are explicit, not invented.

Origin metadata is optional, bounded, and namespaced. Study Hub can include its lecture/course/exam identity and slide/transcript role under its own namespace. Another app may use its own external document ID and tags. Neither slide/transcript pairs nor course/exam fields are required by Clinical Notebook. The old proposed `study-hub-send-v1.schema.json` remains a historical text-pair proposal, not the generic source API contract.

Identical import retries return the same receipt and source identity. The same idempotency key with a changed payload is a conflict. Validate actual file bytes, supported type, size, and hashes before recording receipt success. Recover interrupted imports without duplicating persisted sources. A multi-file client reports each result separately; a partial transfer cannot be shown as wholly ready.

Reading existing content must not change the user's recently viewed list or initiate extraction/model work. Original-file export and extracted-text reads are distinct operations. Bind responses to content revision; a revision-constrained read either returns that revision or explicitly reports that it is unavailable, never a silently substituted newer version.

## Processing independent of model credentials

Preserving originals, listing metadata, local supported text extraction, reading available text, and exporting files must work with no NotebookLM or AI-provider credential. The access token authorizes the user's own Clinical Notebook installation; it is not an AI-provider API key.

Some existing extraction options invoke model-assisted figure descriptions or embeddings. Integration imports default to local processing only. Model-assisted processing requires an explicit owner-approved processing policy and available provider configuration; an integration cannot enable or broaden that policy. Missing optional processing must appear as a truthful limitation and must not turn successfully stored files into failed transfers. Keep stored, readable, indexed, and ready distinct so clients can decide which state they need. Source transport never triggers quiz/lesson generation or adoption.

## Connections and application lifecycle

Keep the source API independent from transport configuration. Default desktop exposure is loopback while the app is running, with a supported endpoint discovery mechanism that does not expose the native administrative credential. Publish the base URL and API version independently of private runtime implementation details. A connection knows which Clinical Notebook installation it targets.

Remote integrations such as a Hub on another computer need a supported reachable deployment. Provide an explicitly enabled authenticated HTTPS integration endpoint or a configured private proxy to the same scoped API. Expose only integration routes, not the whole internal backend. Tailscale may be used for one installation, but no Tailscale hostname, NUC path, Cloudflare account, owner email, or fixed private port belongs in the generic contract. Browser-origin policy is explicit and does not replace token authorization. Do not automatically expose a listener to the public network.

For a desktop receiver that cannot accept inbound connections, an outbound pull adapter may retrieve an explicitly requested export from a configured application origin and feed the same authorized import boundary. The adapter authenticates as a named integration and rechecks its current notebook/capability grants and revocation at import admission; it never imports under the desktop administrative identity. That is a transport adapter, not a second privileged source API. A custom URL can wake the local app; it carries an opaque action/transfer ID, not a bearer or arbitrary URL. If used for Study Hub, its adapter must be independently configured and revocable. Arbitrary source-URL fetching and a general callback/synchronization framework are not required for the first external API.

App availability stays visible. Closing a desktop window and quitting the application have different effects; a closed app cannot be described as an always-on server. The Study Hub client owns its pending-send queue and can retry when the receiver is available. The deployment must prove one complete supported cross-computer path before the Hub integration is called accepted. No cloud relay or account service is implied by distributing the app.

## Study Hub as first client

Keep course/exam mapping, selection of current approved PDF/cleaned transcript, one-click send controls, immutable export preparation, and outgoing receipts in Study Hub. Clinical Notebook selects/authorizes the target notebooks and grants the integration's source capabilities. The Hub uses the same public import/read APIs as a second application; it gets no special backend bypass.

The immediate workflow still sends the actual slide PDF and cleaned transcript, permits slides-only material when that is what was selected, resumes partial transfers, and does not block Hub upload completion on notebook delivery. Display the destination and per-file state. Sending is explicit; a source edit or delete on either side never silently propagates to the other.

## Acceptance and distribution proof

- Run the same contract against Study Hub and an unrelated minimal client using separate tokens and separate grants.
- Import a nonmedical text document with no Hub/course/exam/slide/transcript metadata; read its text and export a separate original file through authorized endpoints.
- Prove import-only, read-only, original-export permission, revocation, inaccessible IDs, source relinking, shared-source association filtering, pagination scope, token namespace separation, and denial of administrative routes.
- Verify external reads have no recently-viewed or model-call side effects and omit local paths and secrets.
- Complete a file/text import-read-export round trip with no AI-provider credentials configured and no model calls. Test unavailable local extraction and optional processing independently from byte-transfer success.
- Exercise replay, conflicting payload, malformed type/size/hash, interrupted transfer, content revision changes, and partial batch recovery.
- Test default local access and the actual selected remote connection path; verify the private desktop administrative backend remains private.
- Verify a fresh installation can create its own connections from documentation, with no personal hostname, account, preexisting token, private folder, or Study Hub dependency.

Sharing the application publicly, distribution signing, multi-user hosting, managed OAuth registration, a connector marketplace, and paid hosting are separate future decisions. The API and documentation built now should make another person's independent installation practical without requiring those systems.
