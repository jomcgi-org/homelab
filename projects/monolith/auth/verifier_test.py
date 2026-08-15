"""Hermetic tests for standing-token verification and resolver ordering."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from auth.errors import AuthError, AuthErrorReason
from auth.principal import Authority, Principal, PrincipalKind
from auth.settings import AuthSettings
from auth.verifier import AuthentikStandingVerifier, TokenResolver

ISSUER = "https://auth.example/application/o/mcp-friends/"
AUDIENCE = "https://private.example"
JWKS_URL = f"{ISSUER}jwks/"


class Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _settings(**overrides) -> AuthSettings:
    settings = AuthSettings(
        authentik_jwks_url=JWKS_URL,
        authentik_issuer=ISSUER,
        authentik_audience=AUDIENCE,
        jwks_cache_ttl_s=300,
    )
    return replace(settings, **overrides)


def _key_and_jwk(kid: str = "key-1"):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return private_key, jwk


def _token(private_key, *, kid: str = "key-1", **claim_overrides) -> str:
    claims = {
        "sub": "user-123",
        "email": "person@example.com",
        "groups": ["friends", "operators"],
        "scope": "openid profile tools:read",
        "iss": ISSUER,
        "aud": ["another-audience", AUDIENCE],
        "exp": int(time.time()) + 3600,
    }
    claims.update(claim_overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _fetcher(*documents):
    calls: list[str] = []

    async def fetch(url: str):
        calls.append(url)
        index = min(len(calls) - 1, len(documents) - 1)
        return documents[index]

    return fetch, calls


@pytest.mark.asyncio
async def test_valid_human_token_maps_all_identity_facts():
    private_key, jwk = _key_and_jwk()
    fetch, calls = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    principal = await verifier.verify(_token(private_key))

    assert principal == Principal(
        subject="user-123",
        actor=(),
        scope=("openid", "profile", "tools:read"),
        groups=("friends", "operators"),
        email="person@example.com",
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
    )
    assert principal.has_group("friends")
    assert principal.has_scope("tools:read")
    assert calls == [JWKS_URL]


@pytest.mark.asyncio
async def test_valid_service_account_without_email_is_workload():
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    principal = await verifier.verify(_token(private_key, email=None))

    assert principal is not None
    assert principal.email is None
    assert principal.kind is PrincipalKind.WORKLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"exp": int(time.time()) - 60}, AuthErrorReason.EXPIRED),
        ({"aud": ["somewhere-else"]}, AuthErrorReason.WRONG_AUDIENCE),
    ],
)
async def test_invalid_registered_claims_are_caller_faults(claims, reason):
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key, **claims))

    assert raised.value.reason is reason


@pytest.mark.asyncio
async def test_wrong_issuer_through_resolver_is_unrecognized():
    """Wrong issuer means this token is not ours, so the resolver raises UNRECOGNIZED."""
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    # The verifier DECLINES rather than raising, which is what lets a second
    # verifier see the token at all. Assert both halves: the decline itself, and
    # that a decline with nobody left to ask still fails closed.
    assert (
        await verifier.verify(_token(private_key, iss="https://wrong.example/")) is None
    )

    resolver = TokenResolver([verifier])
    with pytest.raises(AuthError) as raised:
        await resolver.resolve(_token(private_key, iss="https://wrong.example/"))

    assert raised.value.reason is AuthErrorReason.UNRECOGNIZED


@pytest.mark.asyncio
async def test_token_without_exp_is_rejected():
    """A token with no expiry must be refused, not accepted forever.

    PyJWT validates exp only when the claim is PRESENT, so this is enforced by
    options={"require": [...]} rather than by decode's own defaults. Without
    that option every assertion in this file still passes while a non-expiring
    token is honoured indefinitely, which is why this test exists.
    """
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)
    no_exp = jwt.encode(
        {
            "sub": "user-123",
            "email": "person@example.com",
            "iss": ISSUER,
            "aud": [AUDIENCE],
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "key-1"},
    )

    with pytest.raises(AuthError) as raised:
        await verifier.verify(no_exp)

    assert raised.value.reason is AuthErrorReason.MALFORMED
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_bad_signature_is_explicit_caller_fault():
    signing_key, _ = _key_and_jwk()
    _, published_jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [published_jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(signing_key))

    assert raised.value.reason is AuthErrorReason.BAD_SIGNATURE


@pytest.mark.asyncio
async def test_unknown_kid_refreshes_once_and_remains_an_error():
    private_key, _ = _key_and_jwk("missing")
    _, other_jwk = _key_and_jwk("other")
    fetch, calls = _fetcher({"keys": [other_jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key, kid="missing"))

    assert raised.value.reason is AuthErrorReason.UNKNOWN_KID
    assert calls == [JWKS_URL, JWKS_URL]


@pytest.mark.asyncio
async def test_unknown_kid_refresh_can_resolve_rotated_key():
    private_key, wanted_jwk = _key_and_jwk("rotated")
    _, stale_jwk = _key_and_jwk("stale")
    fetch, calls = _fetcher(
        {"keys": [stale_jwk]},
        {"keys": [stale_jwk, wanted_jwk]},
    )
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    principal = await verifier.verify(_token(private_key, kid="rotated"))

    assert principal is not None
    assert principal.subject == "user-123"
    assert calls == [JWKS_URL, JWKS_URL]


@pytest.mark.asyncio
async def test_jwks_fetch_failure_is_infrastructure_fault():
    private_key, _ = _key_and_jwk()

    async def failing_fetch(url: str):
        raise OSError("network unavailable")

    verifier = AuthentikStandingVerifier(_settings(), fetch=failing_fetch)

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key))

    assert raised.value.reason is AuthErrorReason.JWKS_UNREACHABLE
    assert raised.value.status_code == 503
    assert raised.value.headers is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["authentik_jwks_url", "authentik_issuer", "authentik_audience"],
)
async def test_empty_identity_setting_is_explicit_infrastructure_fault(field):
    private_key, jwk = _key_and_jwk()
    fetch, calls = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(
        _settings(**{field: ""}),
        fetch=fetch,
    )

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key))

    assert raised.value.reason is AuthErrorReason.UNCONFIGURED
    assert raised.value.status_code == 503
    assert calls == []


@pytest.mark.asyncio
async def test_jwks_cache_honors_ttl_without_sleeping():
    private_key, jwk = _key_and_jwk()
    fetch, calls = _fetcher({"keys": [jwk]})
    clock = Clock()
    verifier = AuthentikStandingVerifier(
        _settings(jwks_cache_ttl_s=10),
        fetch=fetch,
        now=clock,
    )
    token = _token(private_key)

    await verifier.verify(token)
    clock.advance(9)
    await verifier.verify(token)
    assert calls == [JWKS_URL]

    clock.advance(2)
    await verifier.verify(token)
    assert calls == [JWKS_URL, JWKS_URL]


class StubVerifier:
    def __init__(self, result=None, error: AuthError | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def verify(self, token: str):
        self.calls.append(token)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_present_token_unrecognized_by_every_verifier_is_not_anonymous():
    first = StubVerifier()
    second = StubVerifier()
    resolver = TokenResolver([first, second])

    with pytest.raises(AuthError) as raised:
        await resolver.resolve("opaque-token")

    assert raised.value.reason is AuthErrorReason.UNRECOGNIZED
    assert first.calls == ["opaque-token"]
    assert second.calls == ["opaque-token"]


@pytest.mark.asyncio
async def test_ordered_chain_second_verifier_reached_on_first_decline():
    """The real standing verifier declines a different-issuer token, and the stub is consulted."""
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [jwk]})
    standing = AuthentikStandingVerifier(_settings(), fetch=fetch)

    delegation_principal = Principal(
        subject="delegate",
        actor=("broker",),
        scope=("tools:read",),
        groups=(),
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.DELEGATED,
    )
    stub = StubVerifier(result=delegation_principal)

    delegation_token = _token(private_key, iss="https://delegation-issuer/")
    principal = await TokenResolver([standing, stub]).resolve(delegation_token)

    assert principal is delegation_principal
    assert stub.calls == [delegation_token]


@pytest.mark.asyncio
async def test_ordered_chain_stops_when_first_verifier_raises():
    first = StubVerifier(error=AuthError(AuthErrorReason.BAD_SIGNATURE))
    second = StubVerifier(result="must not be returned")

    with pytest.raises(AuthError) as raised:
        await TokenResolver([first, second]).resolve("bad-standing-token")

    assert raised.value.reason is AuthErrorReason.BAD_SIGNATURE
    assert first.calls == ["bad-standing-token"]
    assert second.calls == []


def _delegation_principal() -> Principal:
    """Stand in for what #4944's verifier will return."""

    return Principal(
        subject="delegate",
        actor=("broker",),
        scope=("tools:read",),
        groups=(),
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.DELEGATED,
    )


def _foreign_token(
    algorithm: str = "HS256", *, iss: str = "https://delegation-issuer/"
) -> str:
    """A token a LATER verifier owns, deliberately not RS256-with-kid."""

    return jwt.encode(
        {"sub": "delegate", "iss": iss}, "shared-secret", algorithm=algorithm
    )


@pytest.mark.asyncio
async def test_non_jwt_credential_declines_to_the_next_verifier():
    """Undecodable is not the same as invalid, and only one of them is ours to say.

    This verifier used to raise MALFORMED for anything it could not decode,
    which made slot 1's opinion final for the whole chain: a later verifier
    owning an opaque credential would never be consulted.
    """
    fetch, calls = _fetcher({"keys": []})
    standing = AuthentikStandingVerifier(_settings(), fetch=fetch)
    delegated = _delegation_principal()
    stub = StubVerifier(result=delegated)

    principal = await TokenResolver([standing, stub]).resolve("opaque-not-a-jwt")

    assert principal is delegated
    assert stub.calls == ["opaque-not-a-jwt"]
    # Ownership was settled without an outbound fetch.
    assert calls == []


@pytest.mark.asyncio
async def test_foreign_issuer_token_of_another_algorithm_declines():
    """The algorithm pin must not gate ownership, only validity.

    #4944's delegation tokens need not be RS256-with-kid. With the pin ahead of
    the ownership gate they would 401 at slot 1, and no later slot could revisit
    that, so the chain would be unreachable for exactly the tokens it exists for.
    """
    fetch, calls = _fetcher({"keys": []})
    standing = AuthentikStandingVerifier(_settings(), fetch=fetch)
    delegated = _delegation_principal()
    stub = StubVerifier(result=delegated)
    foreign = _foreign_token()

    principal = await TokenResolver([standing, stub]).resolve(foreign)

    assert principal is delegated
    assert stub.calls == [foreign]
    assert calls == []


@pytest.mark.asyncio
async def test_token_claiming_our_issuer_is_judged_here_not_passed_on():
    """The gate decides ownership only. A token claiming our issuer is ours to reject."""
    fetch, _ = _fetcher({"keys": []})
    standing = AuthentikStandingVerifier(_settings(), fetch=fetch)
    stub = StubVerifier(result=_delegation_principal())
    owned_but_wrong_algorithm = _foreign_token(iss=ISSUER)

    with pytest.raises(AuthError) as raised:
        await TokenResolver([standing, stub]).resolve(owned_but_wrong_algorithm)

    assert raised.value.reason is AuthErrorReason.MALFORMED
    assert stub.calls == []


@pytest.mark.asyncio
async def test_unconfigured_verifier_raises_even_for_a_token_it_does_not_own():
    """The one deliberate exception to gate-before-raise.

    An unconfigured verifier cannot know what it owns, so declining would let a
    deployment mistake read as "nobody recognized this token" (401) instead of
    the infrastructure fault it is (503).
    """
    fetch, _ = _fetcher({"keys": []})
    standing = AuthentikStandingVerifier(_settings(authentik_issuer=""), fetch=fetch)
    stub = StubVerifier(result=_delegation_principal())

    with pytest.raises(AuthError) as raised:
        await TokenResolver([standing, stub]).resolve(_foreign_token())

    assert raised.value.reason is AuthErrorReason.UNCONFIGURED
    assert raised.value.status_code == 503
    assert stub.calls == []


@pytest.mark.asyncio
async def test_rs256_algorithm_pin_rejects_other_algorithms(caplog):
    """Verify that the RS256 pin is enforced before JWKS lookup."""
    private_key, jwk = _key_and_jwk()
    fetch, calls = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)
    caplog.set_level(logging.DEBUG, logger="monolith.auth")

    # Create a token with HS256 (symmetric key), same structure otherwise.
    token = jwt.encode(
        {
            "sub": "user-123",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "exp": int(time.time()) + 3600,
        },
        "secret-key",
        algorithm="HS256",
        headers={"kid": "key-1"},
    )

    with pytest.raises(AuthError) as raised:
        await verifier.verify(token)

    assert raised.value.reason is AuthErrorReason.MALFORMED
    assert calls == []  # No JWKS fetch should have been attempted


@pytest.mark.asyncio
async def test_not_yet_valid_token_is_caller_fault():
    """Token with nbf in the future is caller error."""
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key, nbf=int(time.time()) + 3600))

    assert raised.value.reason is AuthErrorReason.NOT_YET_VALID
    assert raised.value.status_code == 401


@pytest.mark.asyncio
async def test_non_list_groups_is_malformed():
    """Groups claim must be a list of strings."""
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key, groups="not-a-list"))

    assert raised.value.reason is AuthErrorReason.MALFORMED


@pytest.mark.asyncio
async def test_non_string_scope_is_malformed():
    """Scope claim must be a space-separated string."""
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": [jwk]})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key, scope=["list", "not", "string"]))

    assert raised.value.reason is AuthErrorReason.MALFORMED


@pytest.mark.asyncio
async def test_forced_refresh_rate_limited_by_ttl():
    """Multiple unknown-kid verifications within the TTL produce only one fetch."""
    private_key, jwk = _key_and_jwk()
    fetch, calls = _fetcher({"keys": [jwk]})
    clock = Clock()
    verifier = AuthentikStandingVerifier(
        _settings(jwks_cache_ttl_s=10),
        fetch=fetch,
        now=clock,
    )

    # First unknown kid triggers a refresh.
    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key, kid="missing-1"))
    assert raised.value.reason is AuthErrorReason.UNKNOWN_KID
    assert calls == [JWKS_URL, JWKS_URL]

    # Within the TTL floor, no new fetch.
    clock.advance(5)
    with pytest.raises(AuthError):
        await verifier.verify(_token(private_key, kid="missing-2"))
    assert calls == [JWKS_URL, JWKS_URL]

    # After TTL expires, a new refresh happens.
    clock.advance(6)
    with pytest.raises(AuthError):
        await verifier.verify(_token(private_key, kid="missing-3"))
    assert calls == [JWKS_URL, JWKS_URL, JWKS_URL, JWKS_URL]


@pytest.mark.asyncio
async def test_failed_refresh_cached_within_ttl():
    """A failed JWKS fetch is re-raised quickly within the TTL."""
    private_key, _ = _key_and_jwk()
    call_count = 0

    async def failing_fetch_once(url: str):
        nonlocal call_count
        call_count += 1
        raise OSError("network down")

    clock = Clock()
    verifier = AuthentikStandingVerifier(
        _settings(jwks_cache_ttl_s=10),
        fetch=failing_fetch_once,
        now=clock,
    )

    # First call: the cold-cache fetch fails and raises immediately. ONE attempt,
    # not two: the unknown-kid refresh is never reached, because there is no
    # document to miss in. Hitting a down IdP twice per request would double both
    # its load and the caller's latency at the worst possible moment.
    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key, kid="missing"))
    assert raised.value.reason is AuthErrorReason.JWKS_UNREACHABLE
    assert call_count == 1

    # Within TTL: re-raise the cached error without a new fetch.
    clock.advance(5)
    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key, kid="another"))
    assert raised.value.reason is AuthErrorReason.JWKS_UNREACHABLE
    assert call_count == 1  # Negative cache served it, no new fetch


@pytest.mark.asyncio
async def test_jwks_malformed_is_distinct_from_unreachable():
    """A structurally invalid JWKS is a malformed reason, not unreachable."""
    private_key, jwk = _key_and_jwk()
    fetch, _ = _fetcher({"keys": "not-a-list"})
    verifier = AuthentikStandingVerifier(_settings(), fetch=fetch)

    with pytest.raises(AuthError) as raised:
        await verifier.verify(_token(private_key))

    assert raised.value.reason is AuthErrorReason.JWKS_MALFORMED
    assert raised.value.status_code == 503


@pytest.mark.asyncio
async def test_unparseable_ttl_falls_back_to_default(caplog):
    """An unparseable AUTH_JWKS_CACHE_TTL_S falls back to 300s and logs warning."""
    import logging

    caplog.set_level(logging.WARNING, logger="monolith.auth")

    # Create a settings object with unparseable TTL
    settings = AuthSettings(
        authentik_jwks_url=JWKS_URL,
        authentik_issuer=ISSUER,
        authentik_audience=AUDIENCE,
        jwks_cache_ttl_s=300,  # We'll test via from_env
    )

    # Test from_env behavior with monkeypatch
    import os

    original_getenv = os.getenv

    def mock_getenv(key, default=None):
        if key == "AUTH_JWKS_CACHE_TTL_S":
            return "not-a-number"
        return original_getenv(key, default)

    os.getenv = mock_getenv
    try:
        settings_from_env = AuthSettings.from_env()
        assert settings_from_env.jwks_cache_ttl_s == 300.0
        # Check that warning was logged
        assert any(
            "invalid AUTH_JWKS_CACHE_TTL_S" in record.message
            for record in caplog.records
        )
    finally:
        os.getenv = original_getenv


@pytest.mark.asyncio
async def test_empty_ttl_falls_back_to_default():
    """An empty AUTH_JWKS_CACHE_TTL_S falls back to 300s."""
    import os

    original_getenv = os.getenv

    def mock_getenv(key, default=None):
        if key == "AUTH_JWKS_CACHE_TTL_S":
            return ""
        return original_getenv(key, default)

    os.getenv = mock_getenv
    try:
        settings_from_env = AuthSettings.from_env()
        assert settings_from_env.jwks_cache_ttl_s == 300.0
    finally:
        os.getenv = original_getenv
