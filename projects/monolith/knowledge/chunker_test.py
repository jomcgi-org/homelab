"""Tests for the generic markdown chunker."""

from knowledge.chunker import Chunk, chunk_markdown


class TestChunkMarkdown:
    def test_empty_input_yields_no_chunks(self):
        assert chunk_markdown("") == []

    def test_short_input_yields_single_chunk(self):
        chunks = chunk_markdown("Hello world.")
        assert len(chunks) == 1
        assert chunks[0]["index"] == 0
        assert chunks[0]["section_header"] == ""
        assert "Hello world." in chunks[0]["text"]

    def test_section_headers_carried_through(self):
        md = "# Top\n\nIntro.\n\n## Sub\n\nBody."
        chunks = chunk_markdown(md)
        # Headers retain their leading "#" markers as captured by the parser.
        headers = {c["section_header"] for c in chunks}
        assert any("Top" in h for h in headers) or any("Sub" in h for h in headers)

    def test_index_is_zero_based_and_dense(self):
        md = "# A\n\npara1\n\n## B\n\npara2\n\n## C\n\npara3"
        chunks = chunk_markdown(md)
        assert [c["index"] for c in chunks] == list(range(len(chunks)))

    def test_code_block_kept_intact(self):
        md = "Intro.\n\n```\n[[not_a_link]]\n# not a header\n```\n\nOutro."
        chunks = chunk_markdown(md)
        joined = " ".join(c["text"] for c in chunks)
        assert "[[not_a_link]]" in joined

    def test_oversized_paragraph_word_splits(self):
        """A single paragraph exceeding max_tokens must be word-split."""
        words = [f"word{i:03d}" for i in range(80)]
        content = " ".join(words)
        chunks = chunk_markdown(content, max_tokens=20, min_tokens=5)
        # More than one chunk proves the word-split branch fired.
        assert len(chunks) > 1
        # Every original word is preserved across the chunks.
        rejoined = " ".join(c["text"] for c in chunks)
        assert set(rejoined.split()) == set(words)
        # No chunk wildly exceeds max_tokens (allow small boundary overflow).
        max_tokens = 20
        for c in chunks:
            est = int(len(c["text"].split()) * 1.3)
            assert est <= max_tokens * 1.5

    def test_chunk_typeddict_keys_only(self):
        """Narrowed API must not leak storage concerns."""
        chunks = chunk_markdown("Hello.")
        assert set(chunks[0].keys()) == {"index", "section_header", "text"}
        # mypy-ish runtime check: Chunk is a TypedDict with those keys
        sample: Chunk = {"index": 0, "section_header": "", "text": "x"}
        assert sample["index"] == 0
