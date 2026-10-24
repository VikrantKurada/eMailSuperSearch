"""Server-side HTML sanitiser for the message viewer: mail HTML is rebuilt from an allowlist, never passed through.

``sanitize_html`` parses with the stdlib ``html.parser`` and writes a new fragment holding only the tags in
``ALLOWED_TAGS``, none of their attributes except a safe ``href`` on ``a``, and every piece of text escaped. The UI
still renders the result in a sandboxed iframe; this is the first of the two walls, not the only one.

- ``a`` keeps its first ``href`` when, with control characters removed and spaces trimmed, its scheme is ``http``,
  ``https`` or ``mailto``; it then gets ``rel="noopener noreferrer" target="_blank"``. Otherwise it has no attributes.
- ``script``, ``style``, ``iframe``, ``object``, ``svg``, ``noscript`` and ``template`` are dropped with everything in
  them. ``embed`` is a void element, so dropping the tag drops all of it.
- Any other tag (``img``, ``link``, ``meta``, ``form``, ``input``, ``font``, ``html``, ``body`` ...) is dropped and its
  text kept. Comments, doctypes and processing instructions are dropped.
- Unbalanced input is balanced: a closing tag closes every allowed tag opened after its match, a stray closing tag is
  ignored, and whatever is still open at the end is closed.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

# fmt: off
ALLOWED_TAGS = frozenset({
    "p", "br", "div", "span", "b", "i", "u", "strong", "em", "ul", "ol", "li", "table", "thead", "tbody", "tr", "td",
    "th", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "code", "a",
})
# fmt: on
_DROP_WITH_CONTENT = frozenset({"script", "style", "iframe", "object", "svg", "noscript", "template"})
_VOID = frozenset({"br"})
_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_LINK_ATTRS = ' rel="noopener noreferrer" target="_blank"'


def _link_attrs(attrs: list[tuple[str, str | None]]) -> str:
    """The attributes an ``a`` tag keeps: a safe first ``href`` plus ``rel`` and ``target``, else none at all."""
    href = next((value for name, value in attrs if name == "href"), None)
    if href is None:
        return ""
    cleaned = _CONTROL.sub("", href).strip(" ")
    scheme, colon, _ = cleaned.partition(":")
    if not colon or scheme.lower() not in _SAFE_SCHEMES:
        return ""
    return f' href="{escape(cleaned, quote=True)}"{_LINK_ATTRS}'


class _Sanitizer(HTMLParser):
    """Rebuilds a document as an allowlisted fragment; see the module docstring."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._open: list[str] = []
        self._skip_tag: str | None = None  # the drop-with-content tag being skipped, nested ones counted
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _DROP_WITH_CONTENT:
            self._skip_tag, self._skip_depth = tag, 1
            return
        if tag not in ALLOWED_TAGS:
            return
        if tag in _VOID:
            self.out.append(f"<{tag}>")
            return
        self.out.append(f"<{tag}{_link_attrs(attrs) if tag == 'a' else ''}>")
        self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag not in self._open:  # an unknown, void or stray closing tag
            return
        while self._open:
            top = self._open.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._skip_tag is None:
            self.out.append(escape(data, quote=False))

    def result(self) -> str:
        """The fragment, once the input is fed: the parser is flushed and open tags are closed."""
        self.close()
        self.out.extend(f"</{tag}>" for tag in reversed(self._open))
        self._open.clear()
        return "".join(self.out)


def sanitize_html(html: str) -> str:
    """``html`` rebuilt as a safe HTML fragment; see the module docstring for what survives."""
    parser = _Sanitizer()
    parser.feed(html)
    return parser.result()
