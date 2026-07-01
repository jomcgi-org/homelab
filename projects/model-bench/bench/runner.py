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
import re
import shutil
import tempfile
from pathlib import Path

from bench.cache import HARNESS_VERSION
from bench.schema import Attempt, ResultCell


def extract_files(text: str, target_files: list[str]) -> dict[str, str]:
    """Parse model output into {path: content}.

    Rules (in order):
    1. Recognize FILE <path> header lines. A line that is exactly ``FILE <path>``
       starts a block; everything until the next FILE header or EOF is that
       file's content. Strip one leading newline after the header and one
       trailing newline.
    2. If NO FILE headers are found and there is exactly ONE target file, treat
       the whole response as that file's content, first stripping a surrounding
       fenced code block if the whole response is fenced.
    3. Return only keys present in target_files.
    """
    lines = text.split("\n")

    file_blocks: dict[str, list[str]] = {}
    current_file: str | None = None
    current_lines: list[str] = []
    found_headers = False

    for line in lines:
        if line.startswith("FILE ") and len(line) > 5:
            found_headers = True
            if current_file is not None:
                file_blocks[current_file] = current_lines
            current_file = line[len("FILE ") :].strip()
            current_lines = []
        else:
            if current_file is not None:
                current_lines.append(line)

    if current_file is not None:
        file_blocks[current_file] = current_lines

    if found_headers:
        result: dict[str, str] = {}
        for path, block_lines in file_blocks.items():
            if path not in target_files:
                continue
            # Strip one leading newline: with line-based parsing, an empty
            # first element represents the newline immediately after the header.
            if block_lines and block_lines[0] == "":
                block_lines = block_lines[1:]
            content = "\n".join(block_lines)
            # Strip one trailing newline.
            if content.endswith("\n"):
                content = content[:-1]
            result[path] = content
        return result

    # No FILE headers found.
    if len(target_files) == 1:
        content = text
        # Strip surrounding fenced code block if the whole response is fenced.
        stripped = content.strip()
        fence_match = re.match(r"^```[^\n]*\n(.*)\n```$", stripped, re.DOTALL)
        if fence_match:
            content = fence_match.group(1)
        return {target_files[0]: content}

    return {}


def _make_workdir(fixture_dir: Path) -> Path:
    """Return a fresh temp dir that is an exact copy of fixture_dir."""
    dst = Path(tempfile.mkdtemp())
    shutil.copytree(fixture_dir, dst, dirs_exist_ok=True)
    return dst


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
