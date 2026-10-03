"""Bounded ASCII dice formulas and server-owned randomness."""

import re
import secrets
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

MAX_FORMULA_LENGTH = 64
MAX_MODIFIER = 1000
_PATTERN = re.compile(r"([0-9]+)d([0-9]+)(?:(kh|kl)([0-9]+)|(adv|dis))?([+-][0-9]+)?")


class DiceFormulaError(ValueError):
    """A rejected formula, with a reason safe to return to the caller."""


class DiceRng(Protocol):
    def randint(self, a: int, b: int) -> int: ...


class DiceResult(TypedDict):
    formula: str
    total: int
    rolls: list[int]
    kept: list[int]
    modifier: int


@dataclass(frozen=True)
class DiceFormula:
    formula: str
    count: int
    sides: int
    mode: Literal["kh", "kl", "adv", "dis"] | None
    keep: int
    modifier: int


def parse(formula: str) -> DiceFormula:
    """Validate before integer conversion, without Unicode case folding."""
    if len(formula) > MAX_FORMULA_LENGTH:
        raise DiceFormulaError("formula must be at most 64 characters")
    if not formula.isascii():
        raise DiceFormulaError("formula must contain only ASCII characters")
    if "\n" in formula or "\r" in formula:
        raise DiceFormulaError("formula must not contain line breaks")
    normalized = formula.strip().lower()
    match = _PATTERN.fullmatch(normalized)
    if match is None:
        raise DiceFormulaError(
            "expected NdM, optional khK/klK/adv/dis, and optional +K/-K"
        )
    count, sides = int(match[1]), int(match[2])
    if not 1 <= count <= 100:
        raise DiceFormulaError("dice count must be between 1 and 100")
    if not 1 <= sides <= 1000:
        raise DiceFormulaError("die sides must be between 1 and 1000")
    mode = match[3] or match[5]
    keep = int(match[4]) if match[4] is not None else count
    if keep < 1:
        raise DiceFormulaError("keep count must be at least 1")
    if mode in ("adv", "dis") and count != 1:
        raise DiceFormulaError("adv/dis require exactly one die")
    modifier = int(match[6]) if match[6] is not None else 0
    if abs(modifier) > MAX_MODIFIER:
        raise DiceFormulaError("modifier magnitude must be at most 1000")
    return DiceFormula(normalized, count, sides, mode, min(keep, count), modifier)


def get_dice_rng() -> DiceRng:
    """FastAPI override seam; production never accepts a client seed."""
    return secrets.SystemRandom()


def roll(formula: str, rng: DiceRng | None = None) -> DiceResult:
    parsed = parse(formula)
    source = get_dice_rng() if rng is None else rng
    count = 2 if parsed.mode in ("adv", "dis") else parsed.count
    rolls = [source.randint(1, parsed.sides) for _ in range(count)]
    if parsed.mode in ("kh", "adv"):
        kept = sorted(rolls, reverse=True)[: parsed.keep]
    elif parsed.mode in ("kl", "dis"):
        kept = sorted(rolls)[: parsed.keep]
    else:
        kept = rolls.copy()
    return {
        "formula": parsed.formula,
        "total": sum(kept) + parsed.modifier,
        "rolls": rolls,
        "kept": kept,
        "modifier": parsed.modifier,
    }
