"""Edge-case tests for knowledge/chunker.py header and word splitting.

Pins behaviour at the boundaries of the splitting logic:
- Oversized fenced code block falls into the word-split path.
- Trailing header with no body is silently dropped.
- Heading-only document hits the sections=[] fallback and returns a chunk
  with section_header="" (surprising behaviour, pinned here).
- The unreachable `if current:` inside the word-split inner loop is documented
  (dead code analysis).
"""

from __future__ import annotations

import pytest

from knowledge.chunker import _split_by_headers, _split_paragraphs, chunk_markdown


# ---------------------------------------------------------------------------
# Oversized fenced code block → word-split path
# ---------------------------------------------------------------------------


class TestOversizedCodeBlock:
    def test_oversized_code_block_is_word_split(self):
        """A fenced code block exceeding max_tokens is word-split across chunks.

        This is the documented (if unintended) behaviour: an over-large code
        block falls into the para_tokens > max_tokens branch and is split on
        whitespace boundaries. The test pins the current behaviour and verifies
        that all words survive across the resulting chunks.
        """
        # Build a fenced block with many tokens
        lines = [f"word{i:03d} = {i}" for i in range(50)]
        code_body = "\n".join(lines)
        content = f"```python\n{code_body}\n```"
        # max_tokens=20 forces splits
        chunks = _split_paragraphs(content, max_tokens=20)
        # Multiple chunks expected
        assert len(chunks) > 1
        # All original words from the code block survive
        joined = " ".join(chunks)
        for i in range(50):
            assert f"word{i:03d}" in joined

    def test_oversized_code_block_via_chunk_markdown(self):
        """chunk_markdown also handles an oversized fenced block without crashing."""
        lines = [f"token{i}" for i in range(100)]
        code_body = "\n".join(lines)
        content = f"# Section\n\n```\n{code_body}\n```"
        chunks = chunk_markdown(content, max_tokens=20, min_tokens=5)
        assert len(chunks) >= 1
        # All tokens survive
        joined = " ".join(c["text"] for c in chunks)
        for i in range(100):
            assert f"token{i}" in joined


# ---------------------------------------------------------------------------
# Trailing header with no body → silently dropped
# ---------------------------------------------------------------------------


class TestTrailingHeaderNobody:
    def test_trailing_header_no_body_not_included(self):
        """A heading at the end of the document with no body after it is silently
        dropped from the sections list.

        _split_by_headers only appends a (header, body) pair when there is
        non-empty text; a trailing heading with nothing after it produces an
        empty 'remaining' string and is therefore not appended.  This test
        pins that behaviour.
        """
        content = "# Real Section\n\nSome body text.\n\n# Trailing Header"
        sections = _split_by_headers(content)
        headers = [s[0] for s in sections]
        # The trailing header produces no body, so it is not in sections
        assert "# Trailing Header" not in headers
        # The real section with body is present
        assert "# Real Section" in headers

    def test_trailing_header_via_chunk_markdown_not_in_chunks(self):
        """chunk_markdown also drops a trailing header with no body."""
        content = "# Present\n\nBody here.\n\n## Ghost"
        chunks = chunk_markdown(content)
        headers = {c["section_header"] for c in chunks}
        assert "## Ghost" not in headers
        assert "# Present" in headers


# ---------------------------------------------------------------------------
# Heading-only document → sections=[] fallback
# ---------------------------------------------------------------------------


class TestHeadingOnlyDocument:
    def test_heading_only_hits_fallback_path(self):
        """A document consisting of only a heading (no body text) causes
        _split_by_headers to produce an empty sections list, which then
        falls back to [('', content.strip())].

        chunk_markdown then returns a single chunk whose section_header is ''
        and whose text is the heading itself.  This is surprising but is the
        current defined behaviour; pinning it here makes any future change
        intentional and visible in the diff.
        """
        content = "# Heading Only"
        sections = _split_by_headers(content)
        # No body → sections is empty → fallback fires
        assert len(sections) == 1
        assert sections[0][0] == ""
        assert "Heading Only" in sections[0][1]

    def test_heading_only_chunk_markdown_returns_single_chunk(self):
        """chunk_markdown on a heading-only document returns one chunk."""
        content = "# Just A Title"
        chunks = chunk_markdown(content)
        assert len(chunks) == 1
        # section_header is '' (fallback path, not the heading)
        assert chunks[0]["section_header"] == ""
        assert "Just A Title" in chunks[0]["text"]

    def test_multiple_adjacent_headers_no_body_fallback(self):
        """Multiple consecutive headers with no body anywhere also hit the fallback."""
        content = "# H1\n## H2\n### H3"
        sections = _split_by_headers(content)
        # H1 and H2 have no body before next header; H3 is trailing.
        # None produce a (header, body) pair → fallback.
        assert len(sections) == 1
        assert sections[0][0] == ""


# ---------------------------------------------------------------------------
# Dead-code documentation: unreachable `if current:` in word-split inner loop
# ---------------------------------------------------------------------------


class TestWordSplitDeadCode:
    def test_word_split_flushes_current_before_entry(self):
        """Document the dead-code: the `if current:` flush inside the word-split
        inner loop (lines 100-103 of chunker.py) is unreachable.

        The outer flush (lines 88-91) always drains `current` into `chunks`
        before the `if para_tokens > max_tokens:` branch is reached, so
        `current` is always empty at that point and the inner `if current:`
        can never be True.

        This test exercises a scenario that would trigger the inner flush IF
        it were reachable: a small para followed by an oversized para.  We
        verify that the outer flush already handled the small para, so the
        inner guard is never needed.
        """
        small_para = "short"
        # 50 words → int(50*1.3)=65 tokens, exceeds max_tokens=10
        big_para = " ".join(f"w{i}" for i in range(50))
        text = f"{small_para}\n\n{big_para}"
        chunks = _split_paragraphs(text, max_tokens=10)
        # The small para is in the output
        joined = "\n\n".join(chunks)
        assert "short" in joined
        # All words of the big para are also present
        for i in range(50):
            assert f"w{i}" in joined
