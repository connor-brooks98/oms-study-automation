import hashlib
from pathlib import Path

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    SegmentKind,
    SourceSnapshot,
)
from oms_hub.document_processing.web_adapter import WebProcessor


def _snapshot(path: Path) -> SourceSnapshot:
    return SourceSnapshot(
        id="source-1",
        title="Questions",
        path=path,
        media_type="text/html",
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        original_url="https://professor.example/final",
    )


def test_web_processor_parses_only_stored_visible_content_in_document_order(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.html"
    source.write_text(
        "<html><body><script>ignored()</script><h1>Question set</h1>"
        "<p>Which nerve?</p><form><input value='ignore'></form>"
        "<ul><li>Choice A</li><li>Choice B</li></ul>"
        "<table><tr><td>Answer</td><td>A</td></tr></table>"
        "<img src='/figure.png'><style>.x { color: red }</style></body></html>",
        encoding="utf-8",
    )
    image_path = tmp_path / "assets" / "image.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"image")
    asset = ParsedAsset(
        "web-image-1", image_path, "image/png", "a" * 64, DocumentLocator("web image 1")
    )

    parsed = WebProcessor(_AssetService(asset)).parse(_snapshot(source), tmp_path / "assets")

    assert tuple(segment.kind for segment in parsed.segments) == (
        SegmentKind.HEADING,
        SegmentKind.PARAGRAPH,
        SegmentKind.LIST_ITEM,
        SegmentKind.LIST_ITEM,
        SegmentKind.TABLE,
        SegmentKind.IMAGE,
    )
    assert tuple(segment.text for segment in parsed.segments[:-1]) == (
        "Question set",
        "Which nerve?",
        "Choice A",
        "Choice B",
        "Answer | A",
    )
    assert parsed.segments[-1].asset_keys == ("web-image-1",)
    assert parsed.warnings == ()


def test_web_processor_keeps_text_when_image_snapshot_fails(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.html"
    source.write_text("<p>Question</p><img src='/missing.png'>", encoding="utf-8")

    parsed = WebProcessor(_FailingAssetService()).parse(_snapshot(source), tmp_path / "assets")

    assert tuple(segment.text for segment in parsed.segments) == ("Question",)
    assert parsed.warnings == ("image '/missing.png' could not be snapshotted: unavailable",)


def test_web_processor_downloads_each_resolved_image_url_once(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.html"
    source.write_text("<img src='/figure.png'><img src='https://professor.example/figure.png'>")
    image_path = tmp_path / "assets" / "image.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"image")
    asset = ParsedAsset(
        "web-image-1", image_path, "image/png", "a" * 64, DocumentLocator("web image 1")
    )
    service = _AssetService(asset)

    parsed = WebProcessor(service).parse(_snapshot(source), tmp_path / "assets")

    assert len(service.calls) == 1
    assert len(parsed.assets) == 1
    assert len(parsed.segments) == 2


def test_web_processor_uses_article_content_and_leaf_div_question_blocks(tmp_path: Path) -> None:
    """Blog-style sources commonly put question text directly in nested divs."""
    source = tmp_path / "blogger.html"
    source.write_text(
        "<html><body><aside><p>Popular posts</p></aside>"
        "<div class='post-body entry-content'><div><div>1. First question?</div>"
        "<div>A. First option</div><div>B. Second option</div></div>"
        "<div><div>2. Second question?</div><div>A. Yes</div><div>B. No</div></div>"
        "</div><footer><p>Comments</p></footer></body></html>",
        encoding="utf-8",
    )

    parsed = WebProcessor(_FailingAssetService()).parse(_snapshot(source), tmp_path / "assets")

    assert tuple(segment.text for segment in parsed.segments) == (
        "1. First question?",
        "A. First option",
        "B. Second option",
        "2. Second question?",
        "A. Yes",
        "B. No",
    )


def test_web_processor_keeps_all_16_numbered_blogger_question_blocks(tmp_path: Path) -> None:
    source = tmp_path / "sixteen-questions.html"
    questions = "".join(
        f"<div><div>{number}. Question {number}?</div><div>A. One</div><div>B. Two</div></div>"
        for number in range(1, 17)
    )
    source.write_text(
        f"<html><body><div class='sidebar'><p>Popular posts</p></div>"
        f"<div class='post-body entry-content'>{questions}</div>"
        "<div class='comments'><p>Leave a comment</p></div></body></html>",
        encoding="utf-8",
    )

    parsed = WebProcessor(_FailingAssetService()).parse(_snapshot(source), tmp_path / "assets")

    stems = [segment.text for segment in parsed.segments if segment.text.endswith("?")]
    assert stems == [f"{number}. Question {number}?" for number in range(1, 17)]
    assert all("Popular posts" not in segment.text for segment in parsed.segments)


def test_web_processor_prefers_nested_post_body_over_outer_main_chrome(tmp_path: Path) -> None:
    source = tmp_path / "nested-content-root.html"
    source.write_text(
        "<html><body><main><header><p>Pathology Notes</p></header>"
        "<div class='post-body entry-content'><div>1. First question?</div>"
        "<div>A. One</div><div>B. Two</div></div>"
        "<section class='popular-posts'><p>Popular posts from this blog</p></section>"
        "</main></body></html>",
        encoding="utf-8",
    )

    parsed = WebProcessor(_FailingAssetService()).parse(_snapshot(source), tmp_path / "assets")

    assert tuple(segment.text for segment in parsed.segments) == (
        "1. First question?",
        "A. One",
        "B. Two",
    )


class _AssetService:
    max_bytes = 1024 * 1024

    def __init__(self, asset: ParsedAsset) -> None:
        self.asset = asset
        self.calls: list[tuple[str, str]] = []

    def fetch_asset(
        self, base_url: str, asset_url: str, asset_root: Path, **_: object
    ) -> ParsedAsset:
        self.calls.append((base_url, asset_url))
        return self.asset


class _FailingAssetService:
    max_bytes = 1024 * 1024

    def fetch_asset(
        self, base_url: str, asset_url: str, asset_root: Path, **_: object
    ) -> ParsedAsset:
        raise ValueError("unavailable")
