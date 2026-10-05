"""RULER-style synthetic long-context retrieval (Hsieh et al., 2024), generated locally.

Haystacks are RULER's repeated "noise" sentences, sized with the model's tokenizer so each
prompt lands at a requested context length. Variants:
  single      one needle; return its value
  multikey    the target needle among distractor needles with other keys
  multivalue  four values under one key; all must be returned (score = fraction recalled)
  vt          variable tracking: a chain of assignments; return every variable in the chain
Generation is seeded by (variant, length, index, seed), so every arm sees identical prompts.
"""

import random
import string

from tokenizers import Tokenizer

from .base import Item, Task

NOISE = "The grass is green. The sky is blue. The sun is yellow. Here we go. There and back again."
NIAH_PROMPT = (
    "Some special magic numbers are hidden within the following text. Make sure to memorize it. "
    "I will quiz you about the numbers afterwards.\n\n{context}\n\n"
    "What {what} for {key} mentioned in the provided text? Reply with just the {noun}."
)
VT_PROMPT = (
    "Memorize and track the chain of variable assignments hidden in the following text.\n\n{context}\n\n"
    "Question: Find all variables that are assigned the value {value} in the text above, directly or "
    "through other variables. Reply with just the variable names, separated by spaces."
)
VARIANTS = ("single", "multikey", "multivalue", "vt")
# Tokens left for the chat template and the reply, so prompt + answer fits the requested length.
TEMPLATE_SLACK = 256


def _word(rng, n=8):
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def _number(rng):
    return str(rng.randint(1_000_000, 9_999_999))


class Ruler(Task):
    name = "ruler"
    description = "RULER-style synthetic retrieval at chosen context lengths (needles, variable tracking)"
    defaults = {"temperature": 0.0, "max_tokens": 256, "samples": 1, "thinking": False}
    published = []

    @classmethod
    def add_args(cls, parser):
        g = parser.add_argument_group("ruler")
        g.add_argument("--ruler-tokenizer", help="model tokenizer.json, used to size haystacks (required for ruler)")
        g.add_argument("--ruler-lengths", default="8192,32768", help="comma-separated context lengths in tokens")
        g.add_argument("--ruler-variants", default=",".join(VARIANTS), help=f"subset of {','.join(VARIANTS)}")
        g.add_argument("--ruler-per-length", type=int, default=10, help="prompts per variant per length")
        g.add_argument("--ruler-distractors", type=int, default=8, help="distractor needles for multikey")
        g.add_argument("--ruler-hops", type=int, default=4, help="assignment hops for vt")
        g.add_argument("--ruler-seed", type=int, default=0)

    def load(self, args):
        if not args.ruler_tokenizer:
            raise SystemExit("ruler needs --ruler-tokenizer (the model's tokenizer.json)")
        tok = Tokenizer.from_file(args.ruler_tokenizer)
        noise_tokens = len(tok.encode(" ".join([NOISE] * 100)).ids) / 100
        lengths = [int(x) for x in args.ruler_lengths.split(",") if x.strip()]
        variants = [v.strip() for v in args.ruler_variants.split(",") if v.strip()]
        for v in variants:
            if v not in VARIANTS:
                raise SystemExit(f"unknown ruler variant {v!r}; choose from {VARIANTS}")
        items = []
        for length in lengths:
            for variant in variants:
                for i in range(args.ruler_per_length):
                    rng = random.Random(f"{variant}-{length}-{i}-{args.ruler_seed}")
                    items.append(self._make(tok, noise_tokens, rng, variant, length, i, args))
        return items

    def _make(self, tok, noise_tokens, rng, variant, length, index, args):
        if variant == "vt":
            value = str(rng.randint(10_000, 99_999))
            names = [_word(rng, 5).upper() for _ in range(args.ruler_hops + 1)]
            needles = [f"VAR {names[0]} = {value}."]
            needles += [f"VAR {names[j + 1]} = VAR {names[j]}." for j in range(args.ruler_hops)]
            question = VT_PROMPT.replace("{value}", value)
            target, ordered = names, True
        else:
            key = f"{_word(rng)}-{_word(rng, 4)}"
            if variant == "multivalue":
                values = [_number(rng) for _ in range(4)]
                needles = [f"One of the special magic numbers for {key} is: {v}." for v in values]
                what, noun = "are all the special magic numbers", "numbers"
            else:
                values = [_number(rng)]
                needles = [f"One of the special magic numbers for {key} is: {values[0]}."]
                what, noun = "is the special magic number", "number"
            if variant == "multikey":
                needles += [
                    f"One of the special magic numbers for {_word(rng)}-{_word(rng, 4)} is: {_number(rng)}."
                    for _ in range(args.ruler_distractors)
                ]
            question = NIAH_PROMPT.replace("{what}", what).replace("{key}", key).replace("{noun}", noun)
            target, ordered = values, variant == "multivalue"

        budget = length - TEMPLATE_SLACK - len(tok.encode(question.replace("{context}", "")).ids)
        budget -= sum(len(tok.encode(n).ids) for n in needles)
        n_noise = max(1, int(budget / noise_tokens))
        context = self._haystack(rng, needles, n_noise, keep_order=ordered)
        # One correction pass: tokenization of the joined text differs slightly from the estimate.
        over = len(tok.encode(question.replace("{context}", context)).ids) - (length - TEMPLATE_SLACK)
        if over > 0:
            n_noise = max(1, n_noise - int(over / noise_tokens) - 1)
            context = self._haystack(random.Random(rng.random()), needles, n_noise, keep_order=ordered)
        prompt = question.replace("{context}", context)
        return Item(
            id=f"{variant}-{length}-{index}",
            messages=[{"role": "user", "content": prompt}],
            target=target,
            meta={"group": f"{variant}@{length}", "length": length, "variant": variant},
        )

    @staticmethod
    def _haystack(rng, needles, n_noise, keep_order):
        """Noise sentences with needles at random depths (chains keep their order)."""
        slots = rng.sample(range(n_noise + 1), len(needles))
        if keep_order:
            slots.sort()
        at = {}
        for slot, needle in zip(slots, needles):
            at.setdefault(slot, []).append(needle)
        parts = []
        for i in range(n_noise + 1):
            parts.extend(at.get(i, []))
            if i < n_noise:
                parts.append(NOISE)
        return " ".join(parts)

    def score(self, item, content):
        found = [t for t in item.target if t in content]
        if item.meta["variant"] in ("single", "multikey"):
            return float(bool(found)), " ".join(found)
        return len(found) / len(item.target), " ".join(found)
