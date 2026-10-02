from starlette.requests import Request

from core.identity import verified_email


def _request(*headers: tuple[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers]
    return Request({"type": "http", "headers": raw})


def test_single_projected_header_is_the_identity():
    assert verified_email(_request(("X-Auth-Email", " joe@example.test "))) == (
        "joe@example.test"
    )


def test_spoofable_cf_access_header_is_ignored():
    req = _request(("Cf-Access-Authenticated-User-Email", "forged@example.test"))
    assert verified_email(req) is None


def test_duplicated_projected_header_is_not_attributed():
    req = _request(
        ("X-Auth-Email", "forged@example.test"),
        ("X-Auth-Email", "joe@example.test"),
    )
    assert verified_email(req) is None


def test_missing_or_blank_header_is_none():
    assert verified_email(_request()) is None
    assert verified_email(_request(("X-Auth-Email", "  "))) is None
