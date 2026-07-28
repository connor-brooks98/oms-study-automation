import re
from dataclasses import dataclass
from html import escape

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_LIST_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])\s+(?P<text>.+)$"
)
_RULE = re.compile(r"^\s*(?:-{3,}|_{3,}|\*{3,})\s*$")
_INLINE = re.compile(
    r"(`[^`\n]+`|\*\*[^*\n]+?\*\*|__[^_\n]+?__|"
    r"\*[^*\n]+?\*|_[^_\n]+?_)"
)


@dataclass(frozen=True, slots=True)
class OutlineBlock:
    kind: str
    text: str
    level: int = 0
    marker: str | None = None


def parse_outline_blocks(content: str) -> tuple[OutlineBlock, ...]:
    blocks: list[OutlineBlock] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(
                OutlineBlock(
                    "paragraph",
                    " ".join(paragraph),
                )
            )
            paragraph.clear()

    for raw_line in content.splitlines():
        if not raw_line.strip():
            flush_paragraph()
            continue
        if _RULE.fullmatch(raw_line):
            flush_paragraph()
            blocks.append(OutlineBlock("rule", ""))
            continue
        heading = _HEADING.fullmatch(raw_line)
        if heading is not None:
            flush_paragraph()
            blocks.append(
                OutlineBlock(
                    "heading",
                    heading.group(2),
                    len(heading.group(1)),
                )
            )
            continue
        item = _LIST_ITEM.fullmatch(raw_line)
        if item is not None:
            flush_paragraph()
            indent = item.group("indent").expandtabs(2)
            raw_marker = item.group("marker")
            marker = "•" if raw_marker in {"-", "+", "*"} else raw_marker.rstrip(")")
            if raw_marker.endswith(")"):
                marker += "."
            blocks.append(
                OutlineBlock(
                    "list_item",
                    item.group("text").strip(),
                    len(indent) // 2,
                    marker,
                )
            )
            continue
        paragraph.append(raw_line.strip())

    flush_paragraph()
    return tuple(blocks)


def safe_inline_markup(text: str) -> str:
    rendered: list[str] = []
    cursor = 0
    for match in _INLINE.finditer(text):
        rendered.append(escape(text[cursor : match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            rendered.append(
                f'<font name="Courier">{escape(token[1:-1])}</font>'
            )
        elif token.startswith(("**", "__")):
            rendered.append(f"<b>{escape(token[2:-2])}</b>")
        else:
            rendered.append(f"<i>{escape(token[1:-1])}</i>")
        cursor = match.end()
    rendered.append(escape(text[cursor:]))
    return "".join(rendered)
