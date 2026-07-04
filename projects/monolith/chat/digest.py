"""Chunked window digest -- summary and decisions modes over a message window.

Pure: takes messages and an injectable async LLM caller, returns a string. No
Discord or DB imports, so it needs no session fixture to test. Callers own
fetching the window (chat.store.MessageStore.fetch_window) and building the
caller (chat.summarizer.build_llm_caller); this module only formats and
prompts.
"""

from collections.abc import Awaitable, Callable

from chat.models import Message

# Budget per LLM call, in formatted-text characters. A window under this goes
# out in a single call; a bigger window is split into chunks each under this
# budget, summarized independently, then combined with one reduce call.
_CHUNK_CHARS = 8000

_EMPTY_WINDOW_TEXT = "No messages in the window."

_SUMMARY_SINGLE_PROMPT = (
    "Summarize the following Discord conversation window in 3-6 concise "
    "sentences, covering the main topics discussed and any notable "
    "outcomes. No preamble, no markdown headers.\n\n"
    "Messages:\n{messages}"
)

_SUMMARY_CHUNK_PROMPT = (
    "Summarize this excerpt from a longer Discord conversation in 2-4 "
    "concise sentences, capturing the key topics and outcomes. This is one "
    "piece of a longer window that will be combined with other pieces "
    "later: no preamble, do not refer to it as an excerpt.\n\n"
    "Messages:\n{messages}"
)

_SUMMARY_REDUCE_PROMPT = (
    "The following are summaries of consecutive pieces of a longer Discord "
    "conversation, in chronological order. Combine them into one coherent "
    "3-6 sentence summary of the overall conversation. Do not summarize "
    "each piece separately or refer to them as pieces.\n\n"
    "Piece summaries:\n{chunks}"
)

_DECISIONS_SINGLE_PROMPT = (
    "Review the following Discord conversation window and extract three "
    "things: decisions that were made, action items (noting who said "
    "them), and open questions that were raised but never resolved. If you "
    "cannot tell who is responsible for a decision or action item, leave it "
    "unattributed rather than guessing. Reply with short bullet points "
    "under the headings Decisions, Action Items, and Open Questions; omit a "
    "heading entirely if it has nothing to report.\n\n"
    "Messages:\n{messages}"
)

_DECISIONS_CHUNK_PROMPT = (
    "Review this excerpt from a longer Discord conversation and extract any "
    "decisions, action items (noting who said them), and open questions. If "
    "you cannot tell who is responsible for a decision or action item, "
    "leave it unattributed rather than guessing. This is one piece of a "
    "longer window that will be combined with other pieces later: no "
    "preamble.\n\n"
    "Messages:\n{messages}"
)

_DECISIONS_REDUCE_PROMPT = (
    "The following are decisions, action items, and open questions "
    "extracted from consecutive pieces of a longer Discord conversation, in "
    "chronological order. Combine and deduplicate them into one list under "
    "the headings Decisions, Action Items, and Open Questions. Keep the "
    "attributions given; leave an item unattributed rather than guessing "
    "who is responsible. Omit a heading entirely if it has nothing to "
    "report.\n\n"
    "Extracted pieces:\n{chunks}"
)

_MODE_PROMPTS = {
    "summary": {
        "single": _SUMMARY_SINGLE_PROMPT,
        "chunk": _SUMMARY_CHUNK_PROMPT,
        "reduce": _SUMMARY_REDUCE_PROMPT,
    },
    "decisions": {
        "single": _DECISIONS_SINGLE_PROMPT,
        "chunk": _DECISIONS_CHUNK_PROMPT,
        "reduce": _DECISIONS_REDUCE_PROMPT,
    },
}


def _format_lines(messages: list[Message]) -> list[str]:
    return [
        f"[{m.created_at.strftime('%H:%M')}] {m.username}: {m.content}"
        for m in messages
    ]


def _chunk_lines(lines: list[str], chunk_chars: int) -> list[list[str]]:
    """Group lines into consecutive chunks each under chunk_chars, in order.

    A single line longer than chunk_chars still gets its own chunk (never
    dropped), mirroring MessageStore.fetch_window's newest-always-kept rule.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for line in lines:
        line_chars = len(line) + 1  # +1 for the joining newline
        if current and current_chars + line_chars > chunk_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(line)
        current_chars += line_chars
    if current:
        chunks.append(current)
    return chunks


def _reduce_chunks_text(partials: list[str]) -> str:
    return "\n\n".join(
        f"--- Piece {i + 1} ---\n{partial}" for i, partial in enumerate(partials)
    )


async def digest_window(
    messages: list[Message],
    mode: str,
    caller: Callable[[str], Awaitable[str]],
) -> str:
    """Digest a message window into a summary or a decisions/action-items/
    open-questions report, chunking the prompt when the window is large.

    ``messages`` must be chronological oldest-first (fetch_window's contract).
    The returned string always leads with a coverage line stating how many
    messages were in the window and how far back it reached, so a caller
    relaying this to a user can never silently understate what was covered.
    """
    if mode not in _MODE_PROMPTS:
        raise ValueError(f"unknown digest mode: {mode!r}")
    if not messages:
        return _EMPTY_WINDOW_TEXT

    prompts = _MODE_PROMPTS[mode]
    coverage = (
        f"(window: {len(messages)} messages, "
        f"back to {messages[0].created_at.isoformat()})"
    )

    lines = _format_lines(messages)
    full_text = "\n".join(lines)

    if len(full_text) <= _CHUNK_CHARS:
        body = await caller(prompts["single"].format(messages=full_text))
        return f"{coverage}\n\n{body}"

    chunks = _chunk_lines(lines, _CHUNK_CHARS)
    partials = [
        await caller(prompts["chunk"].format(messages="\n".join(chunk)))
        for chunk in chunks
    ]
    body = await caller(prompts["reduce"].format(chunks=_reduce_chunks_text(partials)))
    return f"{coverage}\n\n{body}"
