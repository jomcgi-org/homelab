"""Small synthetic records using the verified standard release's headers."""

import io

import pytest

from hikes.dobih import build_hills

CSV = """Number, Name, Country, Classification, Metres, Latitude, Longitude
1347,Ben Nevis,S,"Ma,M,Sim",1345,56.79685,-5.00351
2,Overlap,S,"G,D",700,56,-4
3,English hill,E,M,1000,55,-2
4,Irish hill,I,M,1000,55,-6
5,Top,S,MT,1100,57,-4
6,Corbett,S,C,800,57,-5
7,Other,S,Ma,500,57,-4
"""


def test_scotland_classes_and_provenance():
    data = build_hills(io.StringIO(CSV), "18.6")
    assert [h["number"] for h in data["hills"]] == [2, 5, 6, 1347]
    assert data["hills"][0]["classes"] == ["Graham", "Donald"]
    assert data["hills"][-1]["height_m"] == 1345
    assert (
        data["source"]["attribution"] == "The Database of British and Irish Hills v18.6"
    )
    assert data["source"]["license"].endswith("/by/4.0/")
    assert "Filtered" in data["source"]["modifications"]
    assert "joined" in data["source"]["modifications"]


@pytest.mark.parametrize(
    "text,version",
    [
        (CSV, ""),
        ("Name,Country\nA,S", "18.6"),
        (CSV.replace("1345", "NaN"), "18.6"),
        (CSV.replace("1347,Ben", "2,Ben"), "18.6"),
        (CSV.replace(",S,", ",E,"), "18.6"),
    ],
)
def test_rejects_invalid_release(text, version):
    with pytest.raises(ValueError):
        build_hills(io.StringIO(text), version)
