"""Task interface, dataset loading and answer extraction shared by tasks."""

import re
from dataclasses import dataclass, field

import pyarrow.parquet as pq
from huggingface_hub import hf_hub_download


@dataclass
class Item:
    id: str
    messages: list
    target: object
    meta: dict = field(default_factory=dict)


@dataclass
class Published:
    """A score the model's publisher reported, with the protocol it was measured under."""

    value: float  # percent
    metric: str
    protocol: str
    source: str


class Task:
    name = ""
    description = ""
    # Sampling defaults matching the published protocol; every one is overridable from the CLI.
    defaults = {}
    published: list[Published] = []

    @classmethod
    def add_args(cls, parser):
        """Adds task-specific CLI arguments, named --<task name>-... so runs record them."""

    def load(self, args):
        """Returns every Item of the task, in a stable order."""
        raise NotImplementedError

    def score(self, item, content):
        """Returns (score in [0, 1], extracted prediction) for the final answer text."""
        raise NotImplementedError


def parquet_rows(repo, filename, revision=None):
    path = hf_hub_download(repo, filename, repo_type="dataset", revision=revision)
    return pq.read_table(path).to_pylist()


BOXED_INSTRUCTION = "Please reason step by step, and put your final answer within \\boxed{}."


def last_boxed(text):
    """Contents of the last \\boxed{...} in text, with nested braces, or None."""
    start = text.rfind("\\boxed")
    if start < 0:
        return None
    i = text.find("{", start)
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    return None


_NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def normalize_number(s):
    """Canonical string for a numeric answer ('1,000.50' -> '1000.5'), or None."""
    if s is None:
        return None
    s = re.sub(r"\\(?:text|mathrm|textbf)\{([^}]*)\}", r"\1", s)
    s = s.replace("\\$", "").replace("$", "").replace("\\%", "").replace("%", "")
    s = s.replace("\\!", "").replace("{,}", "")
    m = _NUMBER.findall(s)
    if not m:
        return None
    v = m[-1].replace(",", "")
    try:
        f = float(v)
    except ValueError:
        return None
    return str(int(f)) if f == int(f) else str(f)


def numeric_answer(content):
    """The boxed answer as a number, else the last number in the text."""
    boxed = last_boxed(content)
    if boxed is not None:
        return normalize_number(boxed)
    return normalize_number(content)


_CHOICE = re.compile(r"(?i)answer\s*(?:is)?\s*[:：]?\s*\(?\**\s*([A-J])\b")


def choice_answer(content, letters):
    """A multiple-choice letter: boxed, else the last 'Answer: X', else None."""
    boxed = last_boxed(content)
    if boxed is not None:
        m = re.search(r"[A-J]", re.sub(r"\\text\{([^}]*)\}", r"\1", boxed))
        if m and m.group(0) in letters:
            return m.group(0)
    found = [m.group(1).upper() for m in _CHOICE.finditer(content)]
    found = [c for c in found if c in letters]
    return found[-1] if found else None
