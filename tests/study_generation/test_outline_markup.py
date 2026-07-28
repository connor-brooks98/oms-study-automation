from oms_hub.study_generation.outline_markup import (
    parse_outline_blocks,
    safe_inline_markup,
)

MARKDOWN = """# Neurodegeneration

**Core concept:** protein aggregation

- Alzheimer disease
  - **Amyloid-beta** plaques
  - Tau tangles
1. Identify the syndrome
2. Localize the lesion

***

Use `MRI` when indicated.
"""


def test_outline_parser_preserves_hierarchy_and_list_markers():
    blocks = parse_outline_blocks(MARKDOWN)

    assert [
        (block.kind, block.level, block.marker)
        for block in blocks
    ] == [
        ("heading", 1, None),
        ("paragraph", 0, None),
        ("list_item", 0, "•"),
        ("list_item", 1, "•"),
        ("list_item", 1, "•"),
        ("list_item", 0, "1."),
        ("list_item", 0, "2."),
        ("rule", 0, None),
        ("paragraph", 0, None),
    ]


def test_inline_markup_is_allowlisted_and_escapes_raw_html():
    rendered = safe_inline_markup(
        "**Core** and *supporting* with `MRI` <script>alert(1)</script>"
    )

    assert "<b>Core</b>" in rendered
    assert "<i>supporting</i>" in rendered
    assert '<font name="Courier">MRI</font>' in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_unclosed_markers_remain_readable_plain_text():
    rendered = safe_inline_markup("A **partially emphasized statement")

    assert "**partially" in rendered
    assert "<b>" not in rendered
