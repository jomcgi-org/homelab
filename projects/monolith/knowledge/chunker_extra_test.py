"""Additional coverage for the markdown chunker.

Fills gaps not addressed by chunker_test.py:
  - whitespace-only input
  - _estimate_tokens helper (empty, single-word, multi-word)
  - _split_by_headers: preamble content, H4 not a boundary,
    consecutive headers with no body, empty-string fallback
  - _split_paragraphs: unclosed code block flushed to output,
    code block as atomic unit, flush at token boundary
  - chunk_markdown merge logic: same-header small chunks merged via
    repeated-header scenario, different headers never merged,
    chunk >= min_tokens not merged, merge prevented when combined
    exceeds max_tokens, dense index after merges
  - H4 content stays inside parent section text
"""

from __future__ import annotations

import pytest

from knowledge.chunker import (
    _estimate_tokens,
    _split_by_headers,
    _split_paragraphs,
    chunk_markdown,
)


# ---------------------------------------------------------------------------
# _estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty_string_returns_zero(self):
        assert _estimate_tokens("") == 0

    def test_whitespace_only_returns_zero(self):
        # str.split() with no args treats any whitespace as a delimiter
        # and ignores leading/trailing whitespace → empty list.
        assert _estimate_tokens("   \n\n\t  ") == 0

    def test_single_word(self):
        # int(1 * 1.3) == 1
        assert _estimate_tokens("hello") == 1

    def test_three_words(self):
        # int(3 * 1.3) == 3
        assert _estimate_tokens("one two three") == 3

    def test_ten_words(self):
        # int(10 * 1.3) == 13
        assert _estimate_tokens(" ".join(f"w{i}" for i in range(10))) == 13

    def test_formula_matches_word_count(self):
        """Result must equal int(word_count * 1.3) regardless of content."""
        text = "alpha beta gamma delta"
        expected = int(len(text.split()) * 1.3)
        assert _estimate_tokens(text) == expected


# ---------------------------------------------------------------------------
# _split_by_headers
# ---------------------------------------------------------------------------


class TestSplitByHeaders:
    def test_no_headers_returns_single_section_with_empty_header(self):
        sections = _split_by_headers("Plain text with no headers.")
        assert len(sections) == 1
        assert sections[0] == ("", "Plain text with no headers.")

    def test_preamble_before_first_header_gets_empty_header(self):
        content = "Preamble line.\n\n# First Section\n\nBody of first."
        sections = _split_by_headers(content)
        assert sections[0] == ("", "Preamble line.")
        assert sections[1][0] == "# First Section"
        assert "Body of first." in sections[1][1]

    def test_h1_h2_h3_all_captured(self):
        content = "# H1\n\nText1.\n\n## H2\n\nText2.\n\n### H3\n\nText3."
        sections = _split_by_headers(content)
        headers = [s[0] for s in sections]
        assert "# H1" in headers
        assert "## H2" in headers
        assert "### H3" in headers

    def test_h4_not_a_section_boundary(self):
        """#### headers lie outside the #{1,3} range — they must be ignored."""
        content = "#### Deep heading\n\nFollowing text."
        sections = _split_by_headers(content)
        # Entire content forms one section with an empty header.
        assert len(sections) == 1
        assert sections[0][0] == ""
        assert "#### Deep heading" in sections[0][1]

    def test_consecutive_headers_with_no_body_not_added(self):
        """A header immediately followed by another header has an empty body
        (only whitespace between them), so no section is appended for it."""
        content = "# Header1\n\n# Header2\n\nActual body."
        sections = _split_by_headers(content)
        header_names = [s[0] for s in sections]
        assert "# Header1" not in header_names
        assert "# Header2" in header_names

    def test_body_attributed_to_last_header_before_it(self):
        content = "# Section\n\nParagraph one.\n\nParagraph two."
        sections = _split_by_headers(content)
        assert len(sections) == 1
        assert sections[0][0] == "# Section"
        assert "Paragraph one." in sections[0][1]
        assert "Paragraph two." in sections[0][1]

    def test_empty_string_falls_through_to_single_empty_section(self):
        """Empty content produces the no-match fallback: [('', '')]."""
        sections = _split_by_headers("")
        assert sections == [("", "")]


# ---------------------------------------------------------------------------
# _split_paragraphs
# ---------------------------------------------------------------------------


class TestSplitParagraphs:
    def test_single_paragraph_under_limit_stays_whole(self):
        text = "Short paragraph that fits easily."
        chunks = _split_paragraphs(text, max_tokens=200)
        assert len(chunks) == 1
        assert chunks[0] == "Short paragraph that fits easily."

    def test_two_paragraphs_merged_when_both_fit(self):
        text = "First paragraph.\n\nSecond paragraph."
        chunks = _split_paragraphs(text, max_tokens=200)
        assert len(chunks) == 1
        assert "First paragraph." in chunks[0]
        assert "Second paragraph." in chunks[0]

    def test_paragraphs_split_when_combined_exceeds_max(self):
        # 20 words per paragraph → int(20*1.3)=26 tokens each.
        # max_tokens=30: first para (26 tokens) fits, second para would make 52 > 30.
        para1 = " ".join(f"word{i}" for i in range(20))
        para2 = " ".join(f"term{i}" for i in range(20))
        text = f"{para1}\n\n{para2}"
        chunks = _split_paragraphs(text, max_tokens=30)
        assert len(chunks) == 2

    def test_code_block_treated_as_atomic_unit(self):
        """Blank lines inside a fenced code block must not break it apart."""
        text = "```python\nfirst line\n\nsecond line\n```"
        chunks = _split_paragraphs(text, max_tokens=200)
        assert len(chunks) == 1
        assert "first line" in chunks[0]
        assert "second line" in chunks[0]

    def test_unclosed_code_block_flushed_to_output(self):
        """Content inside an unclosed code block must appear in the output."""
        text = "Intro text.\n\n```python\ncode without closing fence"
        chunks = _split_paragraphs(text, max_tokens=200)
        joined = "\n".join(chunks)
        assert "code without closing fence" in joined

    def test_empty_text_returns_no_chunks(self):
        assert _split_paragraphs("", max_tokens=512) == []

    def test_code_block_flushed_to_new_chunk_when_prose_near_limit(self):
        """Prose that fills the token budget is flushed before a code block."""
        # 22 words → int(22*1.3)=28 tokens; code block ≈ 3 tokens; combined 31 > 30.
        prose = " ".join(f"p{i}" for i in range(22))
        text = f"{prose}\n\n```\ncode\n```"
        chunks = _split_paragraphs(text, max_tokens=30)
        assert len(chunks) >= 2


# ---------------------------------------------------------------------------
# chunk_markdown — merge logic and additional paths
# ---------------------------------------------------------------------------


class TestChunkMarkdownWhitespace:
    def test_whitespace_only_returns_empty(self):
        assert chunk_markdown("   ") == []
        assert chunk_markdown("\n\n\n") == []
        assert chunk_markdown("\t  \n") == []


class TestChunkMarkdownMerge:
    def test_small_chunks_with_same_repeated_header_are_merged(self):
        """When the same heading appears twice each with a tiny body,
        the second (small) chunk is merged back into the first."""
        # Repeated headings create two raw_chunks with identical headers.
        # _estimate_tokens("two") == 1 < min_tokens=50 and combined fits max.
        content = "# Section\n\none two three\n\n# Section\n\nfour"
        chunks = chunk_markdown(content, max_tokens=512, min_tokens=50)
        section_chunks = [c for c in chunks if "# Section" in c["section_header"]]
        assert len(section_chunks) == 1
        assert "one two three" in section_chunks[0]["text"]
        assert "four" in section_chunks[0]["text"]

    def test_chunks_with_different_headers_never_merged(self):
        """Tiny chunks from different headings must remain separate."""
        content = "# Alpha\n\ntiny\n\n# Beta\n\ntiny"
        chunks = chunk_markdown(content, max_tokens=512, min_tokens=50)
        alpha_chunks = [c for c in chunks if "# Alpha" in c["section_header"]]
        beta_chunks = [c for c in chunks if "# Beta" in c["section_header"]]
        assert len(alpha_chunks) >= 1
        assert len(beta_chunks) >= 1

    def test_merge_skipped_when_chunk_meets_min_tokens(self):
        """A paragraph at or above min_tokens is NOT merged with its predecessor.

        Both paragraphs produce separate raw_chunks (split because their
        combined token count exceeds max_tokens=100).  The second chunk is
        int(40*1.3)=52 tokens which is ≥ min_tokens=50, so the merge
        condition fails.
        """
        # 40 words → int(40 * 1.3) = 52 tokens ≥ min_tokens=50.
        para1 = " ".join(f"alpha{i}" for i in range(40))
        para2 = " ".join(f"beta{i}" for i in range(40))
        content = f"# Section\n\n{para1}\n\n{para2}"
        # max_tokens=100 forces a split: 52+52=104 > 100.
        chunks = chunk_markdown(content, max_tokens=100, min_tokens=50)
        section_chunks = [c for c in chunks if "# Section" in c["section_header"]]
        assert len(section_chunks) == 2

    def test_merge_prevented_when_combined_would_exceed_max_tokens(self):
        """Merge is skipped when the result would exceed max_tokens.

        first_para = 14 words = 18 tokens (fits in max_tokens=20 alone).
        second_para = 3 words = 3 tokens (< min_tokens=10 → wants to merge).
        Combined = 17 words = int(17*1.3)=22 > max_tokens=20 → merge blocked.
        """
        first_para = " ".join(f"word{i}" for i in range(14))
        second_para = "short two three"
        content = f"# Section\n\n{first_para}\n\n{second_para}"
        chunks = chunk_markdown(content, max_tokens=20, min_tokens=10)
        section_chunks = [c for c in chunks if "Section" in c["section_header"]]
        assert len(section_chunks) == 2

    def test_index_dense_after_merge(self):
        """chunk indexes are 0-based and contiguous even after merges."""
        content = "# A\n\nabc\n\n# B\n\nxyz\n\n# C\n\none\n\ntwo"
        chunks = chunk_markdown(content, max_tokens=512, min_tokens=50)
        indices = [c["index"] for c in chunks]
        assert indices == list(range(len(chunks)))


class TestChunkMarkdownEdgeCases:
    def test_content_with_no_headers_uses_empty_section_header(self):
        content = "Just plain text with no headers at all."
        chunks = chunk_markdown(content)
        assert len(chunks) == 1
        assert chunks[0]["section_header"] == ""
        assert chunks[0]["text"] == "Just plain text with no headers at all."

    def test_h4_header_stays_inside_parent_section_text(self):
        """H4 inside a section is not a section boundary; it stays in the text."""
        content = "# Top\n\n#### Not a section break\n\nStill in Top."
        chunks = chunk_markdown(content)
        top_chunks = [c for c in chunks if "# Top" in c["section_header"]]
        assert len(top_chunks) == 1
        assert "#### Not a section break" in top_chunks[0]["text"]

    def test_multi_section_document_preserves_all_words(self):
        """No words are lost when splitting a multi-section document."""
        sections_text = []
        all_words: set[str] = set()
        for i in range(4):
            words = [f"unique{i}_{j}" for j in range(10)]
            all_words.update(words)
            sections_text.append(f"## Section {i}\n\n" + " ".join(words))
        content = "\n\n".join(sections_text)
        chunks = chunk_markdown(content, max_tokens=512)
        found_words: set[str] = set()
        for c in chunks:
            found_words.update(c["text"].split())
        assert all_words <= found_words

    def test_hash_comment_inside_code_block_becomes_section_header(self):
        """Document a known limitation: _split_by_headers runs a regex over the
        entire raw content before _split_paragraphs interprets code fences.
        A '# comment' line inside a fenced block is therefore matched as a
        section boundary.  This test pins the current behaviour so that any
        future fix is deliberate and visible in the diff.
        """
        content = "# Real Header\n\nIntro.\n\n```python\n# not a section header\ncode()\n```\n\nOutro."
        chunks = chunk_markdown(content)
        headers = {c["section_header"] for c in chunks}
        # Current behaviour: the hash-comment inside the fence is treated as a
        # section boundary, producing TWO distinct section headers.
        assert "# Real Header" in headers
        assert "# not a section header" in headers

    def test_every_chunk_has_correct_typeddict_shape(self):
        chunks = chunk_markdown("# Title\n\nSome content here.")
        assert len(chunks) >= 1
        for c in chunks:
            assert set(c.keys()) == {"index", "section_header", "text"}
            assert isinstance(c["index"], int)
            assert isinstance(c["section_header"], str)
            assert isinstance(c["text"], str)

    @pytest.mark.parametrize(
        "max_tok,min_tok",
        [
            (5, 1),  # Very small max forces many chunks.
            (10000, 1),  # Very large max keeps everything in one chunk.
        ],
    )
    def test_extreme_token_limits_produce_valid_dense_index(
        self, max_tok: int, min_tok: int
    ) -> None:
        content = "# A\n\n" + " ".join(f"w{i}" for i in range(50))
        chunks = chunk_markdown(content, max_tokens=max_tok, min_tokens=min_tok)
        assert len(chunks) >= 1
        assert [c["index"] for c in chunks] == list(range(len(chunks)))
