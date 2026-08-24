"""Tests for goosecracker.repo_catalog: the invoker-scoped repo menu the
DeepSeek orchestrator uses to select the goose brief's repo."""

from goosecracker.repo_catalog import REPO_CATALOG, describe_repos


def test_menu_lists_only_granted_repos():
    menu = describe_repos(frozenset({"jomcgi-org/homelab"}))
    assert "- jomcgi-org/homelab = " in menu
    # A repo the invoker does not hold is never offered.
    assert "weave-hand/loom" not in menu


def test_menu_carries_the_catalog_description():
    menu = describe_repos(frozenset({"weave-hand/loom"}))
    assert "- weave-hand/loom = " in menu
    assert "Loom" in menu


def test_known_repos_render_in_catalog_order():
    menu = describe_repos(frozenset({"weave-hand/loom", "jomcgi-org/homelab"}))
    # Catalog order (homelab before loom) is preserved regardless of set order.
    assert menu.index("jomcgi-org/homelab") < menu.index("weave-hand/loom")


def test_granted_but_uncatalogued_repo_still_appears():
    # A grant is never hidden: an id not in the catalog gets a generic line so
    # the model can still select a valid repo it holds.
    menu = describe_repos(frozenset({"someone/newrepo"}))
    assert "- someone/newrepo = (no description on file)" in menu


def test_known_repos_lead_uncatalogued_follow():
    menu = describe_repos(frozenset({"someone/newrepo", "jomcgi-org/homelab"}))
    assert menu.index("jomcgi-org/homelab") < menu.index("someone/newrepo")


def test_no_grants_renders_none_sentinel():
    menu = describe_repos(frozenset())
    assert menu.startswith("(none")


def test_deterministic_for_a_given_scope_set():
    scopes = frozenset({"jomcgi-org/homelab", "weave-hand/loom", "z/z", "a/a"})
    assert describe_repos(scopes) == describe_repos(scopes)


def test_catalog_ids_are_owner_slash_repo():
    # Every catalog key is an owner/repo id (the ADR 029 scope shape), so a
    # menu line is directly usable as the brief's repo and the mirror path.
    for rid, entry in REPO_CATALOG.items():
        assert rid == entry.id
        assert rid.count("/") == 1 and all(rid.split("/"))
