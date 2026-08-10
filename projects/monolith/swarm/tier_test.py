from app.modules_public import PUBLIC_MODULES


def test_swarm_is_not_in_public_registry():
    assert "swarm" not in {module.name for module in PUBLIC_MODULES}
