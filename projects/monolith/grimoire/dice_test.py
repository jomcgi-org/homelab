"""Go parity and deterministic property checks without extra dependencies."""

import random
import secrets

import pytest

from grimoire.dice import DiceFormulaError, get_dice_rng, parse, roll


@pytest.mark.parametrize(
    "formula,minimum,maximum",
    [
        ("2d6", 2, 12),
        ("1d20", 1, 20),
        ("1d6+3", 4, 9),
        ("1d6-2", -1, 4),
        ("1d20adv", 1, 20),
        ("1d20dis", 1, 20),
        ("4d6kh3", 3, 18),
        ("4d6kl3", 3, 18),
        ("2d6kh5", 2, 12),
        ("2d6kl5", 2, 12),
        ("2D6", 2, 12),
        ("1d1", 1, 1),
        ("100d1000", 100, 100000),
        ("1d1+1000", 1001, 1001),
        ("1d1-1000", -999, -999),
        ("1d1+0", 1, 1),
        ("1d1-0", 1, 1),
    ],
)
def test_go_cases_and_extensions(formula, minimum, maximum):
    rng = random.Random(6611)
    for _ in range(30):
        result = roll(formula, rng)
        assert set(result) == {"formula", "total", "rolls", "kept", "modifier"}
        assert minimum <= result["total"] <= maximum
        assert result["total"] == sum(result["kept"]) + result["modifier"]


@pytest.mark.parametrize(
    "formula",
    [
        "", "notdice", "d20", "2d", "abc", "1d1000000", "101d6",
        "0d6", "1d0", "2d20adv", "2d20dis", "1d6kh0", "1d6kl0",
        "1d6+1001", "1d6-1001", "1d6+1-1", "1d6kh1kl1",
        "٣d6", "1d٦", "1d6kh١", "1d6+١", "1d6Kh1", "1d6\n",
        "1d6\r", "1 d6", "1d 6", "1d6 kh1", "1d6+ 1",
        "1d6" + " " * 62, "9" * 65,
    ],
)
def test_invalid_formulas_have_reasons(formula):
    with pytest.raises(DiceFormulaError) as exc:
        roll(formula)
    assert str(exc.value)


def test_normalization_and_raw_length_limit():
    assert roll(" \t2D6KH1+3\t ", random.Random(1))["formula"] == "2d6kh1+3"
    assert parse("1d1" + " " * 61).formula == "1d1"
    with pytest.raises(DiceFormulaError, match="at most 64"):
        parse("1d1" + " " * 62)
    # Even the longest numeric components are bounded before conversion.
    with pytest.raises(DiceFormulaError, match="count"):
        parse("9" * 62 + "d1")


@pytest.mark.parametrize("mode,selector", [("adv", max), ("dis", min)])
def test_advantage_draws_two_and_keeps_one(mode, selector):
    result = roll("1d20" + mode, random.Random(42))
    assert result["rolls"] == [4, 1]
    assert result["kept"] == [selector(result["rolls"])]
    assert result["total"] == selector(result["rolls"])


def test_seed_reproducibility_and_known_result():
    expected = {
        "formula": "4d6kh3+2", "total": 15,
        "rolls": [6, 1, 1, 6], "kept": [6, 6, 1], "modifier": 2,
    }
    assert roll("4d6kh3+2", random.Random(42)) == expected
    assert roll("4d6kh3+2", random.Random(42)) == expected
    assert isinstance(get_dice_rng(), secrets.SystemRandom)


def test_generated_valid_formulas():
    generator = random.Random(6611)
    rng = random.Random(6607)
    for _ in range(750):
        mode = generator.choice([None, "kh", "kl", "adv", "dis"])
        count = 1 if mode in ("adv", "dis") else generator.randint(1, 100)
        sides = generator.randint(1, 1000)
        keep = generator.randint(1, 150) if mode in ("kh", "kl") else count
        modifier = generator.randint(-1000, 1000)
        suffix = (mode + str(keep) if mode in ("kh", "kl") else mode) or ""
        formula = f"{count}d{sides}{suffix}{modifier:+d}"
        result = roll(formula, rng)
        kept_count = min(keep, count)
        assert kept_count + modifier <= result["total"] <= kept_count * sides + modifier
        assert len(result["rolls"]) == (2 if mode in ("adv", "dis") else count)
        assert len(result["kept"]) == kept_count
        assert all(1 <= value <= sides for value in result["rolls"])
        if mode in ("kh", "adv"):
            assert result["kept"] == sorted(result["rolls"], reverse=True)[:kept_count]
        elif mode in ("kl", "dis"):
            assert result["kept"] == sorted(result["rolls"])[:kept_count]
        else:
            assert result["kept"] == result["rolls"]
        assert result["total"] == sum(result["kept"]) + modifier
        assert result["modifier"] == modifier


def test_generated_invalid_formulas():
    generator = random.Random(6612)
    for _ in range(400):
        count, sides = generator.randint(1, 100), generator.randint(1, 1000)
        formula = generator.choice([
            f"{count + 100}d{sides}", f"{count}d{sides + 1000}",
            f"{count}d{sides}kh0", f"{count}d{sides}kl0",
            f"{count}d{sides}+{generator.randint(1001, 100000)}",
            f"{count}d{sides}-{generator.randint(1001, 100000)}",
            f"{count}d{sides}!", f"{count} d{sides}",
        ])
        with pytest.raises(DiceFormulaError) as exc:
            parse(formula)
        assert str(exc.value)
