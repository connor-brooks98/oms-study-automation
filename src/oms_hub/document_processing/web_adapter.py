"""Stored-HTML parsing without active-content execution."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser, Node

from oms_hub.document_processing.domain import (
    DocumentLocator,
    ParsedAsset,
    ParsedDocument,
    ParsedSegment,
    SegmentKind,
    SourceSnapshot,
)


class AssetSnapshotter(Protocol):
    max_bytes: int

    def fetch_asset(
        self, base_url: str, asset_url: str, asset_root: Path, *, max_bytes: int | None = None
    ) -> ParsedAsset: ...


class WebProcessor:
    """Normalize a saved HTML response; it never fetches or executes the page itself."""

    name = "web"
    version = "1"

    def __init__(self, snapshot_service: AssetSnapshotter) -> None:
        self.snapshot_service = snapshot_service

    def supports(self, snapshot: SourceSnapshot) -> bool:
        return (
            snapshot.media_type.split(";", 1)[0].casefold().strip() == "text/html"
            and snapshot.path.suffix.casefold() in {".htm", ".html"}
        )

    def parse(self, snapshot: SourceSnapshot, asset_root: Path) -> ParsedDocument:
        if not self.supports(snapshot):
            raise ValueError(f"web processor does not support source {snapshot.path.name!r}")
        try:
            stored_html = snapshot.path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("stored HTML is not valid UTF-8") from error
        document = HTMLParser(stored_html)
        for node in document.css("script, style, form, template, noscript, iframe, object, embed"):
            node.decompose()
        body = document.body if document.body is not None else document.root
        if body is None:
            raise ValueError("stored HTML has no document root")
        content_root = _content_root(body)
        for node in content_root.css(
            "nav, aside, footer, [role='navigation'], [role='complementary']"
        ):
            node.decompose()
        for node in content_root.css("[class], [id]"):
            attributes = node.attributes
            marker = " ".join(
                str(attributes.get(name) or "") for name in ("class", "id")
            ).casefold()
            if any(
                excluded in marker
                for excluded in (
                    "sidebar",
                    "comment",
                    "social",
                    "share",
                    "related-post",
                    "popular-post",
                )
            ):
                node.decompose()
        segments: list[ParsedSegment] = []
        assets: list[ParsedAsset] = []
        asset_keys: set[str] = set()
        assets_by_resolved_url: dict[str, ParsedAsset] = {}
        warnings: list[str] = []
        remaining_bytes = max(self.snapshot_service.max_bytes - snapshot.path.stat().st_size, 0)
        for node in _nodes_in_document_order(content_root):
            tag = (node.tag or "").casefold()
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table"}:
                text = _visible_text(node) if tag != "table" else _table_text(node)
                if text:
                    segments.append(
                        ParsedSegment(
                            key=f"block-{len(segments) + 1}",
                            kind=_segment_kind(tag),
                            text=text,
                            locator=DocumentLocator(
                                f"block {len(segments) + 1}", block_index=len(segments) + 1
                            ),
                        )
                    )
            elif tag == "div" and _is_leaf_text_div(node):
                text = _visible_text(node)
                if text:
                    segments.append(
                        ParsedSegment(
                            key=f"block-{len(segments) + 1}",
                            kind=SegmentKind.PARAGRAPH,
                            text=text,
                            locator=DocumentLocator(
                                f"block {len(segments) + 1}", block_index=len(segments) + 1
                            ),
                        )
                    )
            elif tag == "img":
                source_url = str(node.attributes.get("src") or "").strip()
                if not source_url or snapshot.original_url is None:
                    continue
                resolved_url = urljoin(snapshot.original_url, source_url)
                if urlparse(resolved_url).scheme not in {"http", "https"}:
                    continue
                try:
                    asset = assets_by_resolved_url.get(resolved_url)
                    if asset is None:
                        asset = self.snapshot_service.fetch_asset(
                            snapshot.original_url,
                            source_url,
                            asset_root,
                            max_bytes=remaining_bytes,
                        )
                        assets_by_resolved_url[resolved_url] = asset
                except (OSError, ValueError) as error:
                    warnings.append(f"image {source_url!r} could not be snapshotted: {error}")
                    continue
                if asset.key not in asset_keys:
                    assets.append(asset)
                    asset_keys.add(asset.key)
                    if asset.path is not None:
                        remaining_bytes = max(remaining_bytes - asset.path.stat().st_size, 0)
                segments.append(
                    ParsedSegment(
                        key=f"block-{len(segments) + 1}-image",
                        kind=SegmentKind.IMAGE,
                        text="",
                        locator=DocumentLocator(
                            f"block {len(segments) + 1}", block_index=len(segments) + 1
                        ),
                        asset_keys=(asset.key,),
                    )
                )
        return ParsedDocument(
            source_id=snapshot.id,
            source_sha256=snapshot.sha256,
            source_format="html",
            parser_name=self.name,
            parser_version=self.version,
            segments=tuple(segments),
            assets=tuple(assets),
            warnings=tuple(warnings),
        )


def _visible_text(node: Node) -> str:
    return " ".join(node.text(separator=" ", strip=True).split())


def _content_root(body: Node) -> Node:
    """Prefer semantic article containers over page chrome.

    Blogger and similar sites often put their post body in a nested ``div``
    rather than paragraphs. Selecting that container before traversal prevents
    navigation, sharing widgets, and comment feeds from becoming source text.
    """
    candidates = _unique_nodes(
        body.css(".post-body, .entry-content, .post-content, .article-body")
    )
    if candidates:
        # A dedicated content class is more reliable than a page-level
        # ``main``/``article`` wrapper, which often also contains headers,
        # related-post modules, or comments. Prefer the deepest matching
        # content root and use text length only to break ties.
        return max(candidates, key=lambda node: (_node_depth(node), len(_visible_text(node))))
    candidates = _unique_nodes(body.css("main, article, [role='main']"))
    if not candidates:
        return body
    return max(candidates, key=lambda node: len(_visible_text(node)))


def _unique_nodes(nodes: list[Node]) -> list[Node]:
    seen: set[int] = set()
    unique: list[Node] = []
    for node in nodes:
        if node.mem_id not in seen:
            seen.add(node.mem_id)
            unique.append(node)
    return unique


def _node_depth(node: Node) -> int:
    depth = 0
    parent = node.parent
    while parent is not None:
        depth += 1
        parent = parent.parent
    return depth


def _is_leaf_text_div(node: Node) -> bool:
    """Keep direct-text divs but never duplicate a nested text block."""
    return not any(
        (child.tag or "").casefold()
        in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "div"}
        for child in _nodes_in_document_order(node, include_root=False)
    )


def _nodes_in_document_order(root: Node, *, include_root: bool = True):
    """Yield only ``root`` and its descendants.

    ``selectolax.Node.traverse`` continues with a node's following siblings,
    which can leak a page footer after a selected article container.
    """
    if include_root:
        yield root
    child = root.child
    while child is not None:
        yield child
        yield from _nodes_in_document_order(child, include_root=False)
        child = child.next


def _table_text(node: Node) -> str:
    rows: list[str] = []
    for row in node.css("tr"):
        cells = [_visible_text(cell) for cell in row.css("th, td")]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _segment_kind(tag: str) -> SegmentKind:
    if tag.startswith("h"):
        return SegmentKind.HEADING
    if tag == "li":
        return SegmentKind.LIST_ITEM
    if tag == "table":
        return SegmentKind.TABLE
    return SegmentKind.PARAGRAPH
