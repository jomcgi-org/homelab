"""2-shot cell runner for model-bench.

For each (task, model) cell: prompt the model, write extracted files into a clean
copy of the frozen fixture, grade with the deterministic verifier, and on failure
feed the verifier's real stderr back for exactly one retry.

Safety invariants:
1. Shot 2 is fed only r1.feedback (verifier stderr). Never the golden/expected answer.
2. The fixture tree is re-copied clean before writing shot-2 files, so a bad
   shot-1 write cannot poison shot 2.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from bench.cache import HARNESS_VERSION
from bench.schema import Attempt, ResultCell


def _strip_code_fence(content: str) -> str:
    """Remove a surrounding markdown code fence if present, tolerating prose
    before or after the fence.

    Models routinely wrap file output in ```lang ... ``` even when asked for raw
    content; leaving the backticks in corrupts the file (yaml.safe_load chokes on
    a leading backtick, compilers reject it). Returns the fenced body, or the
    content trimmed of surrounding blank lines when no fence is present.
    """
    lines = content.split("\n")
    fence_idxs = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("```")]
    if len(fence_idxs) >= 2:
        # Body of the FIRST fenced block (first opening fence to the next fence).
        # Using the first pair, not first-to-last, keeps a trailing example block
        # (e.g. a ```bash one-liner after the file) from leaking into the content.
        return "\n".join(lines[fence_idxs[0] + 1 : fence_idxs[1]])
    if len(fence_idxs) == 1:
        # Only an opening fence survived (e.g. truncated); take everything after.
        return "\n".join(lines[fence_idxs[0] + 1 :]).strip("\n")
    return content.strip("\n")


def _is_file_header(line: str) -> bool:
    """True if a line is a standalone ``FILE <path>`` header.

    Two whitespace-separated tokens where the first is exactly ``FILE``. A real
    source line like ``FILE: x`` (a YAML key) has ``FILE:`` as token 0 and is not
    matched, so we never strip genuine content.
    """
    parts = line.strip().split()
    return len(parts) == 2 and parts[0] == "FILE"


def extract_files(text: str, target_files: list[str]) -> dict[str, str]:
    """Parse model output into {path: content}.

    Single-target tasks (all current ones): the whole response is the file. Drop any
    standalone ``FILE <path>`` header lines and a surrounding code fence, and map the
    result to the one target regardless of the header path -- a model that writes
    ``FILE values.yaml`` instead of the full ``FILE chart/values.yaml`` still lands in
    the right place. This measures capability, not exact envelope format; requiring an
    exact path match silently drops the answer and grades the untouched fixture.

    Multi-target tasks: parse ``FILE <path>`` headers and match each block against
    target_files by exact path, else by basename.
    """
    if len(target_files) == 1:
        body = "\n".join(ln for ln in text.split("\n") if not _is_file_header(ln))
        return {target_files[0]: _strip_code_fence(body)}

    # Multi-target: split on FILE headers.
    file_blocks: dict[str, list[str]] = {}
    current_file: str | None = None
    current_lines: list[str] = []
    for line in text.split("\n"):
        if _is_file_header(line):
            if current_file is not None:
                file_blocks[current_file] = current_lines
            current_file = line.strip().split()[1]
            current_lines = []
        elif current_file is not None:
            current_lines.append(line)
    if current_file is not None:
        file_blocks[current_file] = current_lines

    result: dict[str, str] = {}
    basenames = {t.split("/")[-1]: t for t in target_files}
    for path, block_lines in file_blocks.items():
        content = _strip_code_fence("\n".join(block_lines))
        if path in target_files:
            result[path] = content
        elif path.split("/")[-1] in basenames:
            result[basenames[path.split("/")[-1]]] = content
    return result


def _make_workdir(fixture_dir: Path) -> Path:
    """Return a fresh temp dir that is an exact copy of fixture_dir."""
    dst = Path(tempfile.mkdtemp())
    shutil.copytree(fixture_dir, dst, dirs_exist_ok=True)
    return dst


def _augment_prompt_with_files(
    prompt: str, fixture_dir: Path, target_files: list[str]
) -> str:
    """Append the current contents of the editable files to the prompt.

    A full-file-replacement task asks the model to return the complete updated file,
    but without the current contents the model has nothing to update: a careful model
    refuses (Opus: "returning a fabricated file could drop your existing config") and a
    careless one guesses. Injecting the fixture's current file contents makes the task
    well-posed. Free-text tasks (no target_files) are returned unchanged.
    """
    blocks = []
    for tf in target_files:
        p = fixture_dir / tf
        if p.exists():
            blocks.append(f"FILE {tf}\n{p.read_text()}")
    if not blocks:
        return prompt
    return (
        prompt
        + "\n\nHere are the current contents of the file(s) you may edit. Return the "
        "complete updated file(s), each prefixed with its FILE line:\n\n"
        + "\n\n".join(blocks)
    )


async def run_cell(
    *,
    task_id: str,
    task_version: str,
    model_id: str,
    content_hash: str,
    fixture_dir: Path,
    target_files: list[str],
    prompt: str,
    complete,
    verify,
    verifier_args: dict,
    cost_fn,
    max_tokens: int = 8192,
) -> ResultCell:
    """Run a 2-shot (task, model) cell and return a graded ResultCell.

    Args:
        complete: async (**kwargs) -> Completion; kwargs are model, messages, temperature.
        verify: (workdir: Path, args: dict) -> VerifyResult.
        cost_fn: (prompt_tokens: int, completion_tokens: int) -> float.

    Returns a ResultCell with outcome "pass@1", "pass@2", or "fail".
    """
    workdirs: list[Path] = []
    outcome: str
    attempts: list[Attempt]

    # Give the model the current file contents so it can return a complete update.
    prompt = _augment_prompt_with_files(prompt, fixture_dir, target_files)

    try:
        # Shot 1
        c1 = await complete(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=max_tokens,
        )
        workdir1 = _make_workdir(fixture_dir)
        workdirs.append(workdir1)
        for rel_path, content in extract_files(c1.text, target_files).items():
            dest = workdir1 / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        r1 = verify(workdir1, verifier_args)
        a1 = Attempt(
            passed=r1.passed,
            feedback=r1.feedback,
            latency_ms=c1.latency_ms,
            prompt_tokens=c1.prompt_tokens,
            completion_tokens=c1.completion_tokens,
        )

        if r1.passed:
            outcome = "pass@1"
            attempts = [a1]
        else:
            # Shot 2: feed only the verifier's stderr. Never include the golden answer.
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": c1.text},
                {
                    "role": "user",
                    "content": (
                        "Your previous attempt failed validation. "
                        "Here is the exact validator output:\n\n"
                        f"{r1.feedback}\n\n"
                        "Fix the file(s) and return the complete corrected content."
                    ),
                },
            ]
            c2 = await complete(
                model=model_id,
                messages=messages,
                temperature=0.0,
                max_tokens=max_tokens,
            )
            # Fresh clean copy: shot-1 writes must not leak into shot 2.
            workdir2 = _make_workdir(fixture_dir)
            workdirs.append(workdir2)
            for rel_path, content in extract_files(c2.text, target_files).items():
                dest = workdir2 / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content)
            r2 = verify(workdir2, verifier_args)
            a2 = Attempt(
                passed=r2.passed,
                feedback=r2.feedback,
                latency_ms=c2.latency_ms,
                prompt_tokens=c2.prompt_tokens,
                completion_tokens=c2.completion_tokens,
            )
            outcome = "pass@2" if r2.passed else "fail"
            attempts = [a1, a2]

    finally:
        for wd in workdirs:
            shutil.rmtree(wd, ignore_errors=True)

    total_prompt = sum(a.prompt_tokens for a in attempts)
    total_completion = sum(a.completion_tokens for a in attempts)
    cost = cost_fn(total_prompt, total_completion)
    prompt_template_hash = hashlib.sha256(prompt.encode()).hexdigest()[:8]

    return ResultCell(
        task_id=task_id,
        task_version=task_version,
        model_id=model_id,
        content_hash=content_hash,
        outcome=outcome,
        attempts=attempts,
        cost_usd=cost,
        harness_version=HARNESS_VERSION,
        prompt_template_hash=prompt_template_hash,
    )
