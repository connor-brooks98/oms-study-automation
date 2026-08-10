from __future__ import annotations

import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STUDIO_JAVASCRIPT = REPOSITORY_ROOT / "src/oms_hub/web/static/notebook_studio.js"

FORM_PAGE = """
<!doctype html>
<html lang="en">
  <body>
    <input id="course" value="Neuro">
    <input id="exam" value="1">
    <form data-kind="file">
      <input name="title" value="File reference">
      <input name="file" type="file">
      <select name="role" data-import-role>
        <option value="questions">Questions</option>
        <option value="supporting_reference">Supporting reference</option>
      </select>
      <input name="attach_to_notebook" value="true" data-import-notebook type="checkbox">
    </form>
    <form data-kind="text">
      <input name="title" value="Text reference">
      <textarea name="text">Text facts</textarea>
      <select name="role" data-import-role>
        <option value="questions">Questions</option>
        <option value="supporting_reference">Supporting reference</option>
      </select>
      <input name="attach_to_notebook" value="true" data-import-notebook type="checkbox">
    </form>
    <form data-kind="url">
      <input name="title" value="URL reference">
      <input name="url" value="https://example.test/reference">
      <select name="role" data-import-role>
        <option value="questions">Questions</option>
        <option value="supporting_reference">Supporting reference</option>
      </select>
      <input name="attach_to_notebook" value="true" data-import-notebook type="checkbox">
    </form>
  </body>
</html>
"""


def _assert_native_results(results: list[dict[str, object]]) -> None:
    assert len(results) == 6
    for result in results:
        checked = result["checked"]
        assert isinstance(checked, bool)
        assert result["native_form_data"] is True
        assert result["native_checkbox_present"] is checked
        assert result["role_state"] == {
            "role": "supporting_reference",
            "attach_to_notebook": checked,
        }
        values = result["values"]
        assert isinstance(values, dict)
        assert values["role"] == "supporting_reference"
        assert values["attach_to_notebook"] == str(checked).lower()
        assert values["subject"] == "Neuro"
        assert values["exam_number"] == "1"
        assert values["csrf_token"] == "csrf-token"
        if result["kind"] == "file":
            assert values["file"] == {
                "name": "reference.txt",
                "size": 10,
                "type": "text/plain",
            }


def main() -> None:
    browser_channel = os.environ.get("OMS_HUB_BROWSER_CHANNEL", "chrome")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=browser_channel, headless=True)
        try:
            page = browser.new_page()
            page.set_content(FORM_PAGE)
            page.locator('form[data-kind="file"] input[type="file"]').set_input_files(
                {
                    "name": "reference.txt",
                    "mimeType": "text/plain",
                    "buffer": b"file facts",
                }
            )
            page.evaluate("globalThis.module = { exports: {} }")
            page.add_script_tag(path=str(STUDIO_JAVASCRIPT))
            results = page.evaluate(
                """
                () => {
                  const api = globalThis.module.exports;
                  const course = document.querySelector("#course");
                  const exam = document.querySelector("#exam");
                  const results = [];
                  for (const form of document.querySelectorAll("form[data-kind]")) {
                    const role = form.querySelector("[data-import-role]");
                    const checkbox = form.querySelector("[data-import-notebook]");
                    role.value = "supporting_reference";
                    for (const checked of [true, false]) {
                      checkbox.disabled = false;
                      checkbox.checked = checked;
                      const nativeBefore = new FormData(form);
                      const { body, roleState } = api.buildImportSourceFormData(
                        form, course, exam, "csrf-token",
                      );
                      const values = {};
                      for (const [name, value] of body.entries()) {
                        values[name] = value instanceof File
                          ? { name: value.name, size: value.size, type: value.type }
                          : value;
                      }
                      results.push({
                        kind: form.dataset.kind,
                        checked,
                        native_form_data: body instanceof FormData,
                        native_checkbox_present: nativeBefore.has("attach_to_notebook"),
                        role_state: roleState,
                        values,
                      });
                    }
                  }
                  return results;
                }
                """
            )
            assert isinstance(results, list)
            _assert_native_results(results)
            print(json.dumps(results, sort_keys=True))
            print(
                "NATIVE_BROWSER_FORMDATA_PASS "
                "file/text/url checked=true unchecked=false"
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
