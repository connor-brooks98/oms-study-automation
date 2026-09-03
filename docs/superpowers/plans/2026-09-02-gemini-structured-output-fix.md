# Gemini Structured-Output MIME Enum Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Gemini structured-output request use the REST API's `APPLICATION_JSON` MIME enum without adding model-specific provider code.

**Architecture:** Keep the existing provider-neutral `generate_text(...)` contract and correct the wire translation once inside `GeminiProvider._request(...)`. Strengthen the existing mocked adapter test so an arbitrary model ID receives the exact enum and the caller's canonical schema passes through unchanged.

**Tech Stack:** Python 3.12, httpx, respx, pytest, Ruff, and mypy.

**Spec:** `docs/superpowers/specs/2026-09-02-grouped-matching-and-gemini-structured-output-design.md`, sections 12–15.

## Global Constraints

- Change the raw Gemini GenerateContent REST field `generationConfig.responseFormat.text.mimeType` from `application/json` to exactly `APPLICATION_JSON`.
- Apply the value for every structured-output request; do not branch on `gemini-3.8-flash`, another model ID, or a model allowlist.
- Preserve the provider-neutral `GeminiProvider.generate_text(...)` signature and transmit the caller's schema without mutation.
- Preserve existing unstructured text generation, model listing, generation options, error handling, and response parsing.
- Add no dependency and no schema-normalization layer.
- Automated tests must use mocked HTTP only. Do not call a live provider, retry a production run, deploy, restart, publish, or change provider settings.
- Preserve unrelated working-tree changes.

---

## File structure

- `src/oms_hub/llm/gemini.py` — existing Gemini REST request translation; change its structured-output MIME enum value only.
- `tests/llm/test_gemini.py` — existing mocked provider contract tests; make the structured-generation case model-agnostic and exact.

---

### Task 1: Correct and lock down the Gemini structured-output wire contract

**Files:**
- Modify: `tests/llm/test_gemini.py:88-129`
- Modify: `src/oms_hub/llm/gemini.py:163-169`

**Interfaces:**
- Consumes: `GeminiProvider.generate_text(instruction: str, input_text: str, *, api_key: str, model: str, output_schema: dict[str, object], options: GenerationOptions = DEFAULT_GENERATION_OPTIONS) -> GeneratedText`.
- Produces: the unchanged GenerateContent payload shape with `generationConfig.responseFormat.text.mimeType == "APPLICATION_JSON"` and `schema` equal to the caller's unmodified `output_schema` for any model ID.

- [ ] **Step 1: Replace the loose structured-generation assertion with an exact failing regression test**

Replace `test_gemini_structured_generation_sends_response_format` with this mocked test:

```python
@respx.mock
def test_gemini_structured_generation_sends_response_format() -> None:
    model = "gemini-any-compatible-model"
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"x-request-id": "gemini-json"},
            json={
                "modelVersion": model,
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": '{"answer":"iron"}'}]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 4,
                },
            },
        )
    )
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    original_schema = json.loads(json.dumps(schema))

    result = GeminiProvider().generate_text(
        "Return a grounded answer.",
        "Question",
        api_key="secret",
        model=model,
        output_schema=schema,
    )

    payload = json.loads(route.calls.last.request.content)
    assert payload["generationConfig"]["responseFormat"] == {
        "text": {
            "mimeType": "APPLICATION_JSON",
            "schema": schema,
        }
    }
    assert schema == original_schema
    assert result.text == '{"answer":"iron"}'
```

The arbitrary model ID makes the test fail if the adapter special-cases only a known Gemini version. Exact dictionary equality also prevents the invalid SDK-style string from returning unnoticed and proves that no extra schema rewrite was introduced.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest tests/llm/test_gemini.py::test_gemini_structured_generation_sends_response_format -v
```

Expected: FAIL at the `responseFormat` assertion because the actual `mimeType` is `application/json`, while the expected value is `APPLICATION_JSON`. The mocked route must be called successfully; a route-not-mocked failure means the test URL does not match the arbitrary model argument and must be corrected before implementation.

- [ ] **Step 3: Make the one-line provider-level correction**

In `GeminiProvider._request(...)`, retain the existing conditional and schema object, changing only the enum value:

```python
if output_schema is not None:
    generation_config["responseFormat"] = {
        "text": {
            "mimeType": "APPLICATION_JSON",
            "schema": output_schema,
        }
    }
```

Do not add a constant, helper, SDK dependency, schema sanitizer, or model-name conditional for this single wire literal.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/llm/test_gemini.py::test_gemini_structured_generation_sends_response_format -v
```

Expected: PASS. The captured request contains exactly `APPLICATION_JSON`, the arbitrary model ID is accepted by the adapter, the schema remains equal to its pre-call copy, and the mocked response still parses.

- [ ] **Step 5: Run the complete Gemini adapter regression suite**

Run:

```bash
.venv/bin/pytest tests/llm/test_gemini.py -v
```

Expected: all tests PASS, including connection metadata, unstructured cleaning, prefix order and generation options, unsupported-thinking validation, model listing, authentication redaction, and network-error handling.

- [ ] **Step 6: Run static and diff checks**

Run:

```bash
.venv/bin/ruff check src/oms_hub/llm/gemini.py tests/llm/test_gemini.py
.venv/bin/mypy src/oms_hub/llm/gemini.py
git diff --check
git diff -- src/oms_hub/llm/gemini.py tests/llm/test_gemini.py
```

Expected: Ruff, mypy, and diff hygiene PASS. The final diff contains one production literal change plus the strengthened mocked test, and contains no live request, model-version branch, schema normalizer, dependency change, or unrelated file edit.

- [ ] **Step 7: Commit the bounded fix**

```bash
git add src/oms_hub/llm/gemini.py tests/llm/test_gemini.py
git diff --cached --check
git commit -m "fix(llm): use Gemini structured-output MIME enum"
```

Expected: the commit contains only the Gemini adapter and its mocked regression test. Do not push or use the commit as authorization for live/provider or production actions.
