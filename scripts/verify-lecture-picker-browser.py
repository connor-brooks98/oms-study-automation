"""Local synthetic DOM proof; no server, provider, catalog or Anki mutations."""

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src/oms_hub/web"


def main():
    env = Environment(loader=FileSystemLoader(WEB / "templates"), autoescape=select_autoescape())
    macro = env.get_template("components/lecture_picker.html").module.lecture_picker
    rows = [
        {
            "id": 1000 + index,
            "course": "Neuro" if index < 30 else "Heme",
            "exam": 2 if index % 2 else 3,
            "label": f"Lecture {index:02d} — Reflex source {index}",
        }
        for index in range(60)
    ]
    rows[0]["label"] = '<img src=x onerror="window.unsafe=true">'
    markup = (
        '<form id="single">'
        + str(macro("single-picker", "catalog", required=True))
        + '<button type="submit">Assign</button></form>'
    )
    markup += (
        '<form id="multi">'
        + str(macro("multi-picker", "catalog", name="revision_ids", selected=[1001], multiple=True))
        + "</form>"
    )
    markup += (
        '<script type="application/json" id="catalog">'
        + json.dumps(rows).replace("<", "\\u003c")
        + "</script>"
    )
    with sync_playwright() as engine:
        browser = engine.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            page.set_content(markup)
            for name in ["study-hub.css", "lecture_picker.css"]:
                page.add_style_tag(path=str(WEB / "static" / name))
            page.add_script_tag(path=str(WEB / "static/lecture_picker.js"))
            picker = page.locator("#single-picker")
            assert picker.locator("[data-picker-results] input").count() == 20
            assert not page.eval_on_selector("#single", "form => form.checkValidity()")
            picker.locator("[data-picker-results] input").nth(1).check()
            assert page.eval_on_selector(
                "#single", "form => [...new FormData(form).entries()]"
            ) == [["lecture_id", "1001"]]
            picker.locator("[data-picker-results] input").nth(1).press("ArrowDown")
            assert (
                page.eval_on_selector("#single", 'form => new FormData(form).get("lecture_id")')
                == "1002"
            )
            picker.locator("[data-picker-course]").select_option("Heme")
            assert page.eval_on_selector(
                "#single-picker", "node => node.lecturePicker.getValues()"
            ) == ["1002"]
            picker.locator("[data-picker-exam]").select_option("2")
            picker.locator("[data-picker-search]").fill("source 31")
            assert picker.locator("[data-picker-results] input").count() == 1
            picker.locator("[data-picker-results] input").check()
            assert (
                page.eval_on_selector("#single", 'form => new FormData(form).get("lecture_id")')
                == "1031"
            )
            picker.locator("[data-picker-search]").fill("does not exist")
            assert "No matching lectures" in picker.locator("[data-picker-status]").inner_text()
            assert page.eval_on_selector("#single", "form => form.checkValidity()")
            picker.locator("[data-picker-selection] button").click()
            assert not page.eval_on_selector("#single", "form => form.checkValidity()")
            multi = page.locator("#multi-picker")
            multi.locator("[data-picker-next]").click()
            multi.locator("[data-picker-results] input").first.check()
            assert page.eval_on_selector(
                "#multi", 'form => new FormData(form).getAll("revision_ids")'
            ) == ["1001", "1020"]
            page.eval_on_selector("#multi-picker", "node => node.lecturePicker.setDisabled(true)")
            assert multi.locator("[data-picker-search]").is_disabled()
            page.eval_on_selector("#multi-picker", "node => node.lecturePicker.setDisabled(false)")
            assert not multi.locator("[data-picker-search]").is_disabled()
            assert page.locator("[data-picker-results] img").count() == 0
            assert not page.evaluate("Boolean(window.unsafe)")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            print(
                "PASS bounded results, filters, form values, persistent selection, "
                "keyboard, validation, busy locking, escaped labels and mobile overflow"
            )
        finally:
            browser.close()


if __name__ == "__main__":
    main()
