# AMBOSS access research — 2026-09-11

Decision: AMBOSS publicly advertises an MCP, but the inspected public documentation does not establish a working self-service integration for this personal Study Hub. Do not buy a subscription on the assumption that it includes MCP access. Ask AMBOSS to confirm developer access, permitted use, and AnKing identifier support first.

Scope: public official product/support/legal sources only; no account access, credential inspection, endpoint probing, subscription purchase, or live integration test. Dates below are retrieval dates unless identified as publication dates. “Unknown” means not established by the inspected sources, not that the capability does not exist.

## Evidence matrix

| Topic | Confirmed on 2026-09-11 | Unknown / implication |
| --- | --- | --- |
| MCP existence and content | The [US MCP announcement](https://www.amboss.com/us/newsroom/amboss-mcp), published September 9, 2025, describes agent access to articles, drug monographs, flowcharts, calculators, scores, and patient cases, with source links. | No public endpoint, transport, tool schema, SDK, authentication instructions, developer registration, or operational limits appear on that page. Marketing scope is not a verified tool inventory. |
| MCP eligibility | Both the live US page and [international page](https://www.amboss.com/int/int-newsroom/amboss-mcp) broadly describe availability to AI development teams and autonomous agents. | A search-indexed international excerpt still describes limited development-team access. Current fetched pages omit that restriction. Neither proves open self-service access, individual-student eligibility, or entitlement through a free/paid account. |
| ChatGPT AMBOSS GPT | The [official GPT page](https://www.amboss.com/us/gpt) describes the AMBOSS Medical Knowledge GPT at `go.amboss.com/gpt`, email authorization, and no additional charge. It says other email addresses receive 50 prompts per three months; using the AMBOSS account email lifts that AMBOSS quota, while OpenAI limits remain. Membership supplies library access. | This is a ChatGPT experience, not evidence of an API/MCP credential that Study Hub can reuse. Account-specific behavior was not tested. |
| AI Mode Learning | [Support FAQ](https://support.amboss.com/hc/en-us/articles/43601233276689-AI-Mode-Learnings-FAQs): built into AMBOSS; accepts PDF/DOCX/TXT and JPG/PNG/WEBP uploads, links articles/questions/Anki cards, and currently includes access in membership during testing. URL ingestion is not supported. | Long-term packaging is unsettled; exact prompt/file-size limits and free-account entitlement are not stated. Its uploaded-material and Anki workflows are not documented as MCP tools. |
| Consumer pricing | The [DO clinical student page](https://www.amboss.com/us/students/do/clinical) advertises membership starting at $12.50/month with library, AI Mode, Anki add-on and 50 Qbank questions/month; unlimited Qbank is an add-on. | This is a displayed starting price, not a checkout quote or MCP price. Billing commitment, account offers, MCP charges, and minimum plan need confirmation. |
| AnKing matching | [Official Anki integration](https://www.amboss.com/us/anki) supports Qbank-question-to-AnKing recommendations, card selection, copying an Anki query, and pasting it into Anki Browse. It requires the AnKing Step Deck. | No documented MCP slide-to-card search, NID export schema, deck-version guarantee, GUID mapping, or API for the copy-query workflow was found. AMBOSS Anki integration is real; arbitrary lecture-to-NID automation remains unverified. |
| Content rights | [Terms §§13.1–13.5, 13.8](https://www.amboss.com/us/legal/terms) require written permission outside the stated scope, restrict scraping and external AI/RAG use, and grant active members a revocable personal/internal noncommercial educational license to AI Mode output. Input requires adequate rights; AMBOSS says it excludes Input from LLM training datasets. | Consumer membership is not sufficient evidence of permission to feed retrieved content to Study Hub's model or store it. Request the MCP-specific agreement and rules for citations, excerpts, generated answers, caches, embeddings, and export. |
| Retention | [Privacy policy](https://www.amboss.com/us/legal/privacy-policy-overview), OpenAI API subsection, states that provider processes AMBOSS AI-feature data with zero retention and no training. General deletion policy depends on purpose and legal obligations. | This does not establish zero retention by AMBOSS, every subprocessor, a personal Hub's model provider, or the MCP service. MCP logs/uploads, deletion periods, region, and subprocessors require confirmation. |

## Contact and precise open questions

Recommended recipient: **hello@amboss.com**, asking support to route to the MCP/developer partnerships or product team. This address is explicitly published for questions in the [AI Mode Learning announcement](https://www.amboss.com/us/newsroom/introducing-amboss-ai-mode-learning) and the support FAQ. No dedicated public developer email or MCP onboarding form was verified. Do not guess one.

Suggested subject: **AMBOSS MCP access for a personal medical-study app and AnKing matching**

Explain: individual medical student; existing free AMBOSS account; willing to subscribe to the required plan; private, noncommercial Study Hub; educational questions grounded in AMBOSS with citations; lecture-material-to-existing-AnKing-card matching; no patient data.

1. Can an individual student obtain MCP/API access for this private app? Is it available now, approval-only, or restricted to organizational partners? Does membership include it, and what are the price, quotas, and trial options?
2. Please provide the official endpoint, transport, authentication/registration instructions, tool schemas, examples, and supported third-party client/model-provider requirements.
3. Does MCP expose educational retrieval and AI Mode Learning's uploaded-lecture-to-AnKing recommendations? Can it return existing AnKing note IDs or stable identifiers, with deck version and a mapping to local Anki NIDs? If not, is there another supported API or export?
4. Which license permits using results in a private third-party chatbot? What attribution and source links are required, and may the app retain answers, excerpts, IDs, caches, or embeddings? What changes after cancellation?
5. What input/output and log data does MCP retain, for how long, with which subprocessors and regions? Are uploads used for training, and what deletion controls and file/request limits apply?

## Narrow alternative

[AnkiHub's official site](https://www.ankihub.net/) explicitly markets Smart Search for matching lecture notes, PDFs and videos to flashcards and lists it under Premium/Lifetime. This is a closer documented consumer feature for lecture-to-AnKing selection than the MCP announcement. It does not establish an external API or stable NID export. Evaluate it manually as a comparison, not as an assumed automation backend.

Next integration gate: an official access response or published developer documentation establishing endpoint/auth, eligibility, contract, costs, and returned identifiers. Until then, linking to the official AMBOSS experiences is concrete; implementing a guessed MCP client is premature.
