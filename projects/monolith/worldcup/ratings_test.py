import pytest

from worldcup import ratings


def test_loads_all_48_teams():
    table = ratings.load_elo()
    assert len(table) == 48
    assert table["SCO"] > 0


def test_get_raises_on_unknown_code():
    table = ratings.load_elo()
    with pytest.raises(KeyError):
        ratings.elo_for(table, "ZZZ")
