"""Regression guards for private-tier health composition."""


def test_private_profile_serves_deep_health():
    """The confined monolith must mount /api/health.

    It used to opt out, which made every private-tier register_health component
    dead code: the component composes fine and the endpoint that would run it
    does not exist. The cd component (#4599) shipped that way and never ran.
    """
    from framework import PRIVATE_PROFILE

    # _add_health returns before registering /api/health when this is False,
    # so the flag IS the route's presence. Asserted on the profile rather than
    # by composing the app, which needs the full private dependency set.
    assert PRIVATE_PROFILE.deep_health is True


def test_cd_component_is_registered_on_the_private_tier():
    """Guards the join that broke: component registered, endpoint present."""
    from app.modules_private import ALL_MODULES

    names = {
        n
        for m in ALL_MODULES
        for checks in (m.register_health, m.register_health_advisory)
        if checks
        for n in checks
    }
    assert "cd" in names
