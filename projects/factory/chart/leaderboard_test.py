import os
from pathlib import Path
import runpy

import pytest

snapshot = runpy.run_path(
    os.getenv("SCRIPT", Path(__file__).parent / "files/leaderboard.py")
)["snapshot"]
ROW = dict(arm="control", n=8, merges=4, reverts=0, median_rounds=1, median_minutes=3)


def test_public_shape():
    [arm] = snapshot([ROW])
    assert set(arm) == {
        "arm",
        "n",
        "median_minutes",
        "median_rounds",
        "merge_rate",
        "revert_rate",
    }
    for key in ("merge_rate", "revert_rate"):
        assert set(arm[key]) == {"value", "low", "high"}
    assert arm["merge_rate"] == {"value": 0.5, "low": 0.21522, "high": 0.78478}
    assert snapshot([dict(ROW, merges=0)])[0]["revert_rate"] is None


def test_rejects_private_fields():
    with pytest.raises(ValueError):
        snapshot([dict(ROW, work_item_id=1)])
    with pytest.raises(ValueError):
        snapshot([dict(ROW, median_rounds={"title": "private"})])
