from pathlib import Path
from types import SimpleNamespace

from tools.session_collector.scope import (
    allowed_scope,
    discover_repo,
    homelab_worktrees,
    normalize_origin,
    parse_allowlist,
    parse_path_allowlist,
    reset_worktree_cache,
)


def test_normalizes_ssh_and_https_origins():
    assert normalize_origin("git@github.com:jomcgi-org/homelab.git") == (
        "jomcgi-org/homelab"
    )
    assert normalize_origin("https://github.com/jomcgi/homelab.git") == (
        "jomcgi/homelab"
    )
    assert normalize_origin("ssh://git@github.com/jomcgi/homelab.git") == (
        "jomcgi/homelab"
    )


def test_discovers_origin_for_existing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tools.session_collector.scope.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="git@github.com:jomcgi-org/homelab.git\n"
        ),
    )
    assert discover_repo(str(tmp_path), {}) == "jomcgi-org/homelab"


def test_path_fallback_and_remembered_mapping(tmp_path, monkeypatch):
    missing = tmp_path / "worktrees" / "deleted"
    path_allowlist = {tmp_path / "worktrees": "jomcgi-org/homelab"}
    monkeypatch.setattr(
        "tools.session_collector.scope.homelab_worktrees",
        lambda: frozenset({missing}),
    )
    assert discover_repo(str(missing), {}, path_allowlist) == "jomcgi-org/homelab"
    missing = tmp_path / "homelab" / "deleted"
    assert discover_repo(str(missing), {}, path_allowlist) is None
    state = {"old": {"cwd": str(tmp_path / "other"), "repo": "owner/repo"}}
    assert discover_repo(str(tmp_path / "other"), state, path_allowlist) == "owner/repo"


def test_default_path_fallback_only_matches_configured_prefixes(monkeypatch):
    missing = "/tmp/claude-worktrees/session-collector-deleted"
    monkeypatch.setattr(
        "tools.session_collector.scope.homelab_worktrees",
        lambda: frozenset({Path(missing)}),
    )
    assert discover_repo(str(missing), {}) == "jomcgi-org/homelab"
    unrelated = "/Users/jomcgi/repos/cv/sections/homelab"
    original_is_dir = Path.is_dir
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda path: False if str(path) == unrelated else original_is_dir(path),
    )
    assert discover_repo(unrelated, {}) is None


def test_existing_origin_wins_over_path_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tools.session_collector.scope.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="git@github.com:someone/elsewhere.git\n"
        ),
    )
    repo = discover_repo(str(tmp_path), {}, {tmp_path.parent: "jomcgi-org/homelab"})
    assert repo == "someone/elsewhere"
    assert allowed_scope(repo, parse_allowlist(None)) is None


def test_transcript_origin_wins_over_cwd_probe_and_path_fallback(tmp_path, monkeypatch):
    planted = "cwd-origin-must-not-win"
    monkeypatch.setattr(
        "tools.session_collector.scope.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=f"git@github.com:owner/{planted}.git\n"
        ),
    )
    assert (
        discover_repo(
            str(tmp_path),
            {},
            {tmp_path: "owner/path-fallback"},
            "https://github.com/owner/transcript.git",
        )
        == "owner/transcript"
    )


def test_unregistered_deleted_path_does_not_use_prefix_fallback(tmp_path, monkeypatch):
    planted = tmp_path / "worktrees" / "not-a-real-worktree"
    monkeypatch.setattr(
        "tools.session_collector.scope.homelab_worktrees",
        lambda: frozenset({tmp_path / "worktrees" / "registered"}),
    )
    assert (
        discover_repo(str(planted), {}, {tmp_path / "worktrees": "owner/planted-repo"})
        is None
    )


def test_worktree_registry_is_cached_until_the_next_run(monkeypatch):
    calls = 0

    def worktree_list(*args, **kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            returncode=0,
            stdout="worktree /tmp/codex-worktrees/example\nHEAD abc123\n",
        )

    monkeypatch.setattr("tools.session_collector.scope.subprocess.run", worktree_list)
    reset_worktree_cache()
    assert Path("/tmp/codex-worktrees/example").resolve() in homelab_worktrees()
    assert homelab_worktrees()
    assert calls == 1
    reset_worktree_cache()


def test_allowlist_defaults_and_custom_values():
    defaults = parse_allowlist(None)
    assert defaults["jomcgi/homelab"] == "repo:jomcgi-org/homelab"
    custom = parse_allowlist(["owner/repo=repo:owner/repo"])
    assert custom == {"owner/repo": "repo:owner/repo"}
    assert allowed_scope("outside/repo", defaults) is None
    path_defaults = parse_path_allowlist(None)
    assert path_defaults[Path.home() / "repos/ft-worktrees"] == ("jomcgi-org/homelab")
    assert path_defaults[Path.home() / "repos/homelab"] == "jomcgi-org/homelab"
    assert path_defaults[Path("/tmp/codex-worktrees")] == "jomcgi-org/homelab"
    path_custom = parse_path_allowlist(["~/worktrees=owner/repo"])
    assert path_custom == {Path.home() / "worktrees": "owner/repo"}
