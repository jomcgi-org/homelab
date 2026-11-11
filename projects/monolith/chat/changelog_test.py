"""Tests for the hourly changelog notifier module."""

import json
import os
import re
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from chat.changelog import (
    ChangelogConfig,
    PROMPTS,
    _auth_headers,
    _build_embed,
    _filter_changelog_commits,
    _summarize_with_llm,
    load_changelog_configs,
    run_changelog_for_config,
)


class TestChangelogConfig:
    def test_load_configs_from_json(self):
        """JSON list is parsed into ChangelogConfig objects."""
        raw = json.dumps(
            [
                {
                    "name": "test",
                    "channelId": "123",
                    "githubRepo": "owner/repo",
                    "prompt": "professional",
                    "embedTitle": "Test",
                    "embedColor": "0x2ECC71",
                    "intervalHours": 1,
                }
            ]
        )
        configs = load_changelog_configs(raw)
        assert len(configs) == 1
        assert configs[0].name == "test"
        assert configs[0].github_repo == "owner/repo"
        assert configs[0].channel_id == "123"
        assert configs[0].embed_color == 0x2ECC71
        assert configs[0].interval_hours == 1
        assert configs[0].commit_filter is None
        assert configs[0].roast_chance == 0.0
        assert not configs[0].exclude_dependency_only

    def test_load_configs_with_commit_filter(self):
        """commitFilter string is compiled to a regex pattern."""
        raw = json.dumps(
            [
                {
                    "name": "filtered",
                    "channelId": "123",
                    "githubRepo": "owner/repo",
                    "prompt": "professional",
                    "embedTitle": "Test",
                    "embedColor": "0x2ECC71",
                    "intervalHours": 1,
                    "commitFilter": "^(feat)(\\(.+?\\))?!?:\\s",
                }
            ]
        )
        configs = load_changelog_configs(raw)
        assert configs[0].commit_filter is not None
        assert configs[0].commit_filter.match("feat: something")
        assert not configs[0].commit_filter.match("fix: something")

    def test_load_configs_empty_json(self):
        """Empty JSON list returns empty config list."""
        assert load_changelog_configs("[]") == []

    def test_load_configs_empty_string(self):
        """Empty string returns empty config list."""
        assert load_changelog_configs("") == []

    def test_load_configs_with_roast_chance(self):
        """roastChance is parsed into roast_chance float."""
        raw = json.dumps(
            [
                {
                    "name": "test",
                    "channelId": "123",
                    "githubRepo": "owner/repo",
                    "prompt": "professional",
                    "embedTitle": "Test",
                    "embedColor": "0x2ECC71",
                    "roastChance": 0.1,
                }
            ]
        )
        configs = load_changelog_configs(raw)
        assert configs[0].roast_chance == 0.1

    def test_prompts_registry_has_professional(self):
        """PROMPTS dict contains 'professional' key."""
        assert "professional" in PROMPTS

    def test_prompts_registry_has_roast(self):
        """PROMPTS dict contains 'roast' key."""
        assert "roast" in PROMPTS

    def test_prompts_contain_commits_placeholder(self):
        """All prompts contain {commits} placeholder."""
        for key, prompt in PROMPTS.items():
            assert "{commits}" in prompt, (
                f"Prompt '{key}' missing {{commits}} placeholder"
            )


def _make_commit(message: str, author: str = "Alice") -> dict:
    """Helper to build a minimal GitHub commit dict."""
    return {
        "commit": {
            "message": message,
            "author": {"name": author},
        }
    }


def _make_config(**overrides) -> ChangelogConfig:
    """Helper to build a ChangelogConfig with sensible defaults."""
    defaults = {
        "name": "test",
        "github_repo": "owner/repo",
        "channel_id": "123",
        "prompt": "professional",
        "embed_title": "Test Changelog",
        "embed_color": 0x2ECC71,
        "interval_hours": 1,
        "commit_filter": None,
    }
    defaults.update(overrides)
    return ChangelogConfig(**defaults)


# ---------------------------------------------------------------------------
# _filter_changelog_commits
# ---------------------------------------------------------------------------

_FEAT_ONLY = re.compile(r"^(feat)(\(.+?\))?!?:\s")


class TestFilterChangelogCommits:
    def test_empty_list_returns_empty(self):
        """Empty input returns empty list."""
        assert _filter_changelog_commits([], _FEAT_ONLY) == []

    def test_feat_commit_passes(self):
        """feat: commits are included."""
        commits = [_make_commit("feat: add dark mode")]
        assert len(_filter_changelog_commits(commits, _FEAT_ONLY)) == 1

    def test_fix_commit_excluded(self):
        """fix: commits are excluded (feat-only filter)."""
        commits = [_make_commit("fix: correct typo in footer")]
        assert _filter_changelog_commits(commits, _FEAT_ONLY) == []

    def test_chore_commit_excluded(self):
        """chore: commits are not included."""
        commits = [_make_commit("chore: update dependencies")]
        assert _filter_changelog_commits(commits, _FEAT_ONLY) == []

    def test_docs_commit_excluded(self):
        """docs: commits are not included."""
        commits = [_make_commit("docs: improve README")]
        assert _filter_changelog_commits(commits, _FEAT_ONLY) == []

    def test_refactor_commit_excluded(self):
        """refactor: commits are not included."""
        commits = [_make_commit("refactor: extract helper function")]
        assert _filter_changelog_commits(commits, _FEAT_ONLY) == []

    def test_ci_commit_excluded(self):
        """ci: commits are not included."""
        commits = [_make_commit("ci: add lint step")]
        assert _filter_changelog_commits(commits, _FEAT_ONLY) == []

    def test_feat_with_scope_passes(self):
        """feat(auth): scoped feature commits are included."""
        commits = [_make_commit("feat(auth): add OAuth2 support")]
        assert len(_filter_changelog_commits(commits, _FEAT_ONLY)) == 1

    def test_feat_breaking_change_passes(self):
        """feat!: breaking change feature commits are included."""
        commits = [_make_commit("feat!: redesign auth token format")]
        assert len(_filter_changelog_commits(commits, _FEAT_ONLY)) == 1

    def test_feat_scope_breaking_change_passes(self):
        """feat(auth)!: scoped breaking change is included."""
        commits = [_make_commit("feat(auth)!: drop legacy token support")]
        assert len(_filter_changelog_commits(commits, _FEAT_ONLY)) == 1

    def test_multiline_commit_uses_first_line_only(self):
        """Only the subject line (before first newline) is matched."""
        commits = [_make_commit("feat: add feature\n\nLong description here.")]
        assert len(_filter_changelog_commits(commits, _FEAT_ONLY)) == 1

    def test_multiline_commit_fix_excluded(self):
        """fix: with body is still excluded even when body mentions feat."""
        commits = [_make_commit("fix: patch crash\n\nfeat: unrelated note")]
        assert _filter_changelog_commits(commits, _FEAT_ONLY) == []

    def test_mixed_commits_returns_only_feat(self):
        """Only feat commits are returned from a mixed list."""
        commits = [
            _make_commit("feat: new dashboard"),
            _make_commit("fix: remove null pointer"),
            _make_commit("chore: bump versions"),
            _make_commit("feat(ui): dark mode toggle"),
        ]
        result = _filter_changelog_commits(commits, _FEAT_ONLY)
        assert len(result) == 2
        assert result[0]["commit"]["message"] == "feat: new dashboard"
        assert result[1]["commit"]["message"] == "feat(ui): dark mode toggle"

    def test_partial_word_feat_excluded(self):
        """A commit message starting with 'feature:' (not 'feat:') is excluded."""
        commits = [_make_commit("feature: add something")]
        assert _filter_changelog_commits(commits, _FEAT_ONLY) == []

    def test_feat_without_colon_excluded(self):
        """'feat add something' (no colon) is excluded."""
        commits = [_make_commit("feat add something")]
        assert _filter_changelog_commits(commits, _FEAT_ONLY) == []


# ---------------------------------------------------------------------------
# _build_embed
# ---------------------------------------------------------------------------


class TestBuildEmbed:
    def test_embed_title(self):
        """Embed title is 'Homelab Changelog'."""
        embed = _build_embed(
            "Some summary", commit_count=3, title="Homelab Changelog", color=0x2ECC71
        )
        assert embed.title == "Homelab Changelog"

    def test_embed_color(self):
        """Embed color is 0x2ECC71 (green)."""
        embed = _build_embed(
            "Some summary", commit_count=3, title="Homelab Changelog", color=0x2ECC71
        )
        assert embed.colour.value == 0x2ECC71

    def test_embed_description_matches_summary(self):
        """Embed description equals the provided summary string."""
        summary = "Added dark mode and fixed login bug."
        embed = _build_embed(
            summary, commit_count=2, title="Homelab Changelog", color=0x2ECC71
        )
        assert embed.description == summary

    def test_embed_footer_singular(self):
        """Footer text shows commit count."""
        embed = _build_embed(
            "x", commit_count=1, title="Homelab Changelog", color=0x2ECC71
        )
        assert embed.footer.text == "1 commit(s)"

    def test_embed_footer_plural(self):
        """Footer text with multiple commits."""
        embed = _build_embed(
            "x", commit_count=5, title="Homelab Changelog", color=0x2ECC71
        )
        assert embed.footer.text == "5 commit(s)"

    def test_embed_has_timestamp(self):
        """Embed has a timestamp set."""
        embed = _build_embed(
            "x", commit_count=1, title="Homelab Changelog", color=0x2ECC71
        )
        assert embed.timestamp is not None

    def test_embed_returns_discord_embed(self):
        """Return type is discord.Embed."""
        embed = _build_embed(
            "x", commit_count=1, title="Homelab Changelog", color=0x2ECC71
        )
        assert isinstance(embed, discord.Embed)

    def test_custom_title_and_color(self):
        """Custom title and color are used in the embed."""
        embed = _build_embed(
            "Roast", commit_count=1, title="Colin's Homelab Changelog", color=0xE74C3C
        )
        assert embed.title == "Colin's Homelab Changelog"
        assert embed.colour.value == 0xE74C3C


# ---------------------------------------------------------------------------
# _auth_headers
# ---------------------------------------------------------------------------


class TestAuthHeaders:
    def test_returns_authorization_header(self):
        """Returned dict contains Authorization header with token prefix."""
        headers = _auth_headers("ghp_abc123")
        assert headers["Authorization"] == "token ghp_abc123"

    def test_merges_github_accept_header(self):
        """Returned dict also contains the GitHub Accept header."""
        headers = _auth_headers("tok")
        assert headers["Accept"] == "application/vnd.github+json"

    def test_returns_dict(self):
        """Return value is a plain dict."""
        assert isinstance(_auth_headers("x"), dict)

    def test_does_not_mutate_global_headers(self):
        """Calling _auth_headers does not modify _GITHUB_HEADERS in place."""
        from chat.changelog import _GITHUB_HEADERS

        original_keys = set(_GITHUB_HEADERS.keys())
        _auth_headers("secret")
        assert set(_GITHUB_HEADERS.keys()) == original_keys
        assert "Authorization" not in _GITHUB_HEADERS


# ---------------------------------------------------------------------------
# _summarize_with_llm
# ---------------------------------------------------------------------------


class TestSummarizeWithLlm:
    @pytest.mark.asyncio
    async def test_prompt_includes_commit_messages(self):
        """The prompt forwarded to llm_call contains commit message text."""
        commits = [_make_commit("feat: add search", "Bob")]
        mock_llm = AsyncMock(return_value="Added search functionality.")

        await _summarize_with_llm(commits, mock_llm, PROMPTS["professional"])

        call_args = mock_llm.call_args[0][0]
        assert "feat: add search" in call_args

    @pytest.mark.asyncio
    async def test_prompt_includes_author_names(self):
        """The prompt forwarded to llm_call includes the commit author names."""
        commits = [_make_commit("feat: add feature", "Charlie")]
        mock_llm = AsyncMock(return_value="Added a feature.")

        await _summarize_with_llm(commits, mock_llm, PROMPTS["professional"])

        call_args = mock_llm.call_args[0][0]
        assert "Charlie" in call_args

    @pytest.mark.asyncio
    async def test_returns_llm_output(self):
        """Return value is exactly what the llm_call callable returns."""
        commits = [_make_commit("feat: dark mode")]
        expected = "Users can now toggle dark mode."
        mock_llm = AsyncMock(return_value=expected)

        result = await _summarize_with_llm(commits, mock_llm, PROMPTS["professional"])

        assert result == expected

    @pytest.mark.asyncio
    async def test_llm_called_once(self):
        """llm_call is invoked exactly once per summarize call."""
        commits = [_make_commit("feat: a"), _make_commit("feat: b")]
        mock_llm = AsyncMock(return_value="summary")

        await _summarize_with_llm(commits, mock_llm, PROMPTS["professional"])

        mock_llm.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_commits_all_appear_in_prompt(self):
        """All commit messages appear in the prompt when multiple commits are given."""
        commits = [
            _make_commit("feat: feature A", "Alice"),
            _make_commit("feat: feature B", "Bob"),
        ]
        mock_llm = AsyncMock(return_value="Two features added.")

        await _summarize_with_llm(commits, mock_llm, PROMPTS["professional"])

        call_args = mock_llm.call_args[0][0]
        assert "feat: feature A" in call_args
        assert "feat: feature B" in call_args
        assert "Alice" in call_args
        assert "Bob" in call_args


def _configs_json(name="homelab", channel="999", **extra):
    return json.dumps(
        [
            {
                "name": name,
                "githubRepo": "owner/repo",
                "channelId": channel,
                "prompt": "professional",
                "embedTitle": "Homelab Changelog",
                "embedColor": "2ECC71",
                "intervalHours": 1,
                **extra,
            }
        ]
    )


def _http_client(commits):
    response = MagicMock()
    response.json.return_value = commits
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestRunChangelogForConfig:
    """The off-pod Argo entrypoint: compute the embed and enqueue it (no bot)."""

    @pytest.mark.asyncio
    async def test_enqueues_embed_for_matching_config(self):
        mock_llm = AsyncMock(return_value="Added a great new feature.")
        client = _http_client([_make_commit("feat: amazing thing", "Alice")])
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "ghp_tok", "CHANGELOG_CONFIGS": _configs_json()},
            ),
            patch("chat.changelog.httpx.AsyncClient", return_value=client),
            patch("chat.changelog._enqueue_changelog") as enqueue,
        ):
            await run_changelog_for_config("homelab", mock_llm)

        mock_llm.assert_called_once()
        enqueue.assert_called_once()
        assert enqueue.call_args.args[0] == "999"  # channel_id
        assert isinstance(enqueue.call_args.args[1], dict)  # serialised embed

    @pytest.mark.asyncio
    async def test_no_matching_config_does_not_enqueue(self):
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "ghp_tok", "CHANGELOG_CONFIGS": "[]"},
            ),
            patch("chat.changelog._enqueue_changelog") as enqueue,
        ):
            await run_changelog_for_config("nope", AsyncMock())
        enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_commits_does_not_enqueue(self):
        client = _http_client([])
        with (
            patch.dict(
                "os.environ",
                {"GITHUB_TOKEN": "ghp_tok", "CHANGELOG_CONFIGS": _configs_json()},
            ),
            patch("chat.changelog.httpx.AsyncClient", return_value=client),
            patch("chat.changelog._enqueue_changelog") as enqueue,
        ):
            await run_changelog_for_config("homelab", AsyncMock())
        enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_matching_commits_does_not_enqueue(self):
        mock_llm = AsyncMock()
        client = _http_client([_make_commit("fix: patch null pointer")])
        configs = _configs_json(commitFilter=_FEAT_ONLY.pattern)
        with (
            patch.dict(
                "os.environ", {"GITHUB_TOKEN": "ghp_tok", "CHANGELOG_CONFIGS": configs}
            ),
            patch("chat.changelog.httpx.AsyncClient", return_value=client),
            patch("chat.changelog._enqueue_changelog") as enqueue,
        ):
            await run_changelog_for_config("homelab", mock_llm)
        mock_llm.assert_not_called()
        enqueue.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("roast_chance", "roasted"), [(1.0, True), (0.0, False)])
    async def test_roast_chance_selects_prompt(self, roast_chance, roasted):
        mock_llm = AsyncMock(return_value="Summary.")
        client = _http_client([_make_commit("feat: add thing")])
        configs = _configs_json(roastChance=roast_chance)
        with (
            patch.dict(
                "os.environ", {"GITHUB_TOKEN": "ghp_tok", "CHANGELOG_CONFIGS": configs}
            ),
            patch("chat.changelog.httpx.AsyncClient", return_value=client),
            patch("chat.changelog._enqueue_changelog"),
        ):
            await run_changelog_for_config("homelab", mock_llm)
        prompt_used = mock_llm.call_args[0][0]
        assert ("roasting" in prompt_used) is roasted
        assert ("changelog writer" in prompt_used) is not roasted

    @pytest.mark.asyncio
    async def test_missing_github_token_does_not_enqueue(self):
        with (
            patch.dict(
                "os.environ",
                {"CHANGELOG_CONFIGS": _configs_json()},
                clear=True,
            ),
            patch("chat.changelog._enqueue_changelog") as enqueue,
        ):
            await run_changelog_for_config("homelab", AsyncMock())
        enqueue.assert_not_called()


# Real upstream patches, fetched read-only from ColinCee/homelab.
COLIN_PATCHES = {
    "2e9dfbcf0953605a99373bca70509d9f84a5fdd4": [
        {
            "filename": "ci-runner/versions.env",
            "status": "modified",
            "patch": "@@ -14,7 +14,7 @@ RUNNER_SHA256=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32\n NODE_VERSION=v24.22.0\n NODE_SHA256=cc2f601cee82d9a5a233302fe307abc600727035fd7d2c3a3571755989e95008\n # renovate: datasource=github-releases depName=astral-sh/uv\n-UV_VERSION=0.13.1\n+UV_VERSION=0.13.5\n UV_SHA256=eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220\n PYTHON_VERSION=3.15\n ",
            "additions": 1,
            "deletions": 1,
        }
    ],
    "30bb03404942cd5d7ee861808b2909bc5c041d81": [
        {
            "filename": "stacks/observability/compose.yaml",
            "status": "modified",
            "patch": "@@ -53,7 +53,7 @@ services:\n       - loki-data:/loki\n \n   alloy:\n-    image: grafana/alloy:v1.21.1@sha256:4039c971193053a84e53dd9ed3e7d4561c069256b024631cd3498a353ed7ece8\n+    image: grafana/alloy:v1.21.2@sha256:89a5a320168387336b9ac3c850609c3ba7757eadd4ae5f1d819deb35e9854b18\n     restart: unless-stopped\n     security_opt:\n       - no-new-privileges:true",
            "additions": 1,
            "deletions": 1,
        }
    ],
    "d9efacb3104b477f60142e774f8915da993cc4ce": [
        {
            "filename": "mise.toml",
            "status": "modified",
            "patch": '@@ -1,6 +1,6 @@\n [tools]\n python = "3.15.1"\n-uv = "0.13.4"\n+uv = "0.13.5"\n shellcheck = "0.11.0"\n actionlint = "1.7.12"\n trivy = "0.76.1"',
            "additions": 1,
            "deletions": 1,
        }
    ],
    "e32470c2f1f77fcea03725d0faa484740c528566": [
        {
            "filename": "scripts/server-health.sh",
            "status": "modified",
            "patch": '@@ -116,6 +116,9 @@ done\n \n upgrades="$(apt-get -s upgrade 2>/dev/null | grep -c \'^Inst \' || true)"\n row "Pending apt upgrades" "$upgrades"\n+# Log only: which packages are waiting, and whether Ubuntu is still phasing them in.\n+echo "Pending apt upgrades:"\n+apt-get -s upgrade 2>/dev/null | sed -n \'/^The following/,/^[0-9]* upgraded/p\' || true\n if [[ -f /var/run/reboot-required ]]; then\n   row "Reboot required" "yes (unattended-upgrades reboots at 04:00 UTC)"\n else',
            "additions": 3,
            "deletions": 0,
        }
    ],
    "f394adc2c54b138edf3bd904f5a0823f5e3d2054": [
        {
            "filename": "stacks/flight-tracker/compose.yaml",
            "status": "modified",
            "patch": "@@ -14,7 +14,7 @@ services:\n       - HEATMAP_DB_PATH=/app/data/historical_heatmap.parquet\n \n   cloudflared:\n-    image: cloudflare/cloudflared:2026.10.1@sha256:a15a3b50b8dabd57ac2090e9484c0247dba9dfa1f0df4d298d4317f9d69eb2d5\n+    image: cloudflare/cloudflared:2026.10.2@sha256:0e56c13c7058fcef9c4f0e7d83372f82330548dbf8f8b366f3d8b506526d93b2\n     restart: unless-stopped\n     security_opt:\n       - no-new-privileges:true",
            "additions": 1,
            "deletions": 1,
        }
    ],
}


class TestDependencyOnly:
    @pytest.mark.parametrize("sha", list(COLIN_PATCHES)[:3])
    def test_real_pure_pin_edits(self, sha):
        from chat.changelog import _dependency_only_file

        assert all(_dependency_only_file(f) for f in COLIN_PATCHES[sha])

    def test_real_server_health_code_is_retained(self):
        from chat.changelog import _dependency_only_file

        assert not all(
            _dependency_only_file(f)
            for f in COLIN_PATCHES["e32470c2f1f77fcea03725d0faa484740c528566"]
        )

    @pytest.mark.parametrize(
        "change",
        [
            {"status": "added"},
            {"status": "renamed"},
            {"patch": ""},
            {"additions": 9},
            {"filename": "app.py"},
            {"patch": "@@ -1 +1 @@\n-    privileged: false\n+    privileged: true"},
            {
                "patch": "@@ -1 +1 @@\n-    image: grafana/alloy:v1.0\n+    image: evil/alloy:v1.1"
            },
            {
                "patch": "@@ -1 +1 @@\n-    image: grafana/alloy:v1.0@sha256:"
                + "a" * 64
                + "\n+    image: grafana/alloy:v1.1"
            },
        ],
    )
    def test_unknown_incomplete_and_security_config_changes_stay(self, change):
        from chat.changelog import _dependency_only_file

        file = dict(COLIN_PATCHES["30bb03404942cd5d7ee861808b2909bc5c041d81"][0])
        file.update(change)
        assert not _dependency_only_file(file)

    def test_lock_version_only_but_not_source_change(self):
        from chat.changelog import _dependency_only_file

        file = {
            "filename": "uv.lock",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "patch": '@@ -8 +8 @@\n-version = "1.2.3"\n+version = "1.2.4"',
        }
        assert _dependency_only_file(file)
        file["patch"] = (
            '@@ -8 +8 @@\n-source = { registry = "https://pypi.org/simple" }\n+source = { registry = "https://evil.test/simple" }'
        )
        assert not _dependency_only_file(file)

    def test_mise_non_tools_section_stays(self):
        from chat.changelog import _dependency_only_file

        file = dict(COLIN_PATCHES["d9efacb3104b477f60142e774f8915da993cc4ce"][0])
        file["patch"] = file["patch"].replace("[tools]", "[env]")
        assert not _dependency_only_file(file)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode",
        [
            "pure",
            "mixed",
            "same_file_mixed",
            "security",
            "unavailable",
            "paginated",
            "merge",
            "missing_patch",
        ],
    )
    async def test_real_pipeline_filters_before_summary_and_delivery(self, mode):
        import copy

        import httpx

        sha = "30bb03404942cd5d7ee861808b2909bc5c041d81"
        commit = {
            "sha": sha,
            "commit": {
                "message": "Update grafana/alloy Docker tag to v1.21.2 (#455)",
                "author": {"name": "A human"},
            },
        }
        detail = {
            "sha": sha,
            "parents": [{"sha": "parent"}],
            "files": copy.deepcopy(COLIN_PATCHES[sha]),
        }
        commits = [commit]
        if mode == "mixed":
            # A dependency-titled commit containing substantive code stays whole.
            detail["files"] += COLIN_PATCHES["e32470c2f1f77fcea03725d0faa484740c528566"]
        if mode == "same_file_mixed":
            detail["files"][0]["patch"] += (
                "\n-    privileged: false\n+    privileged: true"
            )
            detail["files"][0]["additions"] += 1
            detail["files"][0]["deletions"] += 1
        if mode == "security":
            commit["commit"]["message"] = (
                "Update cloudflared to 2026.10.2 (eMudhra CA distrust) (#440)"
            )
            detail["files"] = COLIN_PATCHES["f394adc2c54b138edf3bd904f5a0823f5e3d2054"]
        if mode == "merge":
            detail["parents"].append({"sha": "second"})
        if mode == "missing_patch":
            detail["files"][0].pop("patch")

        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path.endswith("/commits"):
                return httpx.Response(200, json=commits)
            if mode == "unavailable":
                return httpx.Response(503)
            headers = (
                {"Link": '<https://api.github.com/next>; rel="next"'}
                if mode == "paginated"
                else {}
            )
            return httpx.Response(200, json=detail, headers=headers)

        raw = json.loads(_configs_json(name="colin-homelab"))
        raw[0]["excludeDependencyOnly"] = True
        raw[0]["githubRepo"] = "ColinCee/homelab"
        config = load_changelog_configs(json.dumps(raw))[0]
        assert config.exclude_dependency_only
        llm = AsyncMock(return_value="Summary")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            context = AsyncMock()
            context.__aenter__.return_value = client
            with (
                patch.dict(
                    os.environ,
                    {"GITHUB_TOKEN": "test", "CHANGELOG_CONFIGS": json.dumps(raw)},
                ),
                patch("chat.changelog.httpx.AsyncClient", return_value=context),
                patch("chat.changelog._enqueue_changelog") as enqueue,
            ):
                await run_changelog_for_config(config.name, llm)
        if mode == "pure":
            llm.assert_not_called()
            enqueue.assert_not_called()
        else:
            llm.assert_awaited_once()
            assert commit["commit"]["message"] in llm.call_args.args[0]
            enqueue.assert_called_once()
        assert len(requests) == (1 if mode == "security" else 2)
        assert requests[0].url.params["sha"] == "main"

    def test_image_switch_to_floating_tag_stays(self):
        from chat.changelog import _dependency_only_file

        file = dict(COLIN_PATCHES["30bb03404942cd5d7ee861808b2909bc5c041d81"][0])
        file["patch"] = (
            "@@ -1 +1 @@\n-    image: grafana/alloy:v1.0\n+    image: grafana/alloy:latest"
        )
        assert not _dependency_only_file(file)

    def test_mise_discontinuous_section_context_stays(self):
        from chat.changelog import _dependency_only_file

        file = dict(COLIN_PATCHES["d9efacb3104b477f60142e774f8915da993cc4ce"][0])
        file["patch"] += '\n@@ -40 +40 @@\n-python = "3.14"\n+python = "3.15"'
        file["additions"] += 1
        file["deletions"] += 1
        assert not _dependency_only_file(file)

    @pytest.mark.asyncio
    async def test_mixed_batch_preserves_order_and_filters_only_proven_pins(self):
        import httpx
        from chat.changelog import _exclude_dependency_commits

        pure_sha, code_sha = (
            list(COLIN_PATCHES)[0],
            "e32470c2f1f77fcea03725d0faa484740c528566",
        )
        commits = [
            {"sha": sha, "commit": {"message": "A human change"}}
            for sha in (pure_sha, code_sha)
        ]

        def handle(request):
            sha = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"sha": sha, "parents": [{}], "files": COLIN_PATCHES[sha]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            config = _make_config(exclude_dependency_only=True)
            assert await _exclude_dependency_commits(
                client, commits, config, "test"
            ) == [commits[1]]

    @pytest.mark.asyncio
    async def test_timeout_retains_remaining_batch_without_retry(self):
        import httpx
        from chat.changelog import _exclude_dependency_commits

        commits = [{"sha": str(i), "commit": {"message": "Change"}} for i in range(3)]
        client = AsyncMock()
        client.get.side_effect = httpx.ReadTimeout("detail lookup stalled")
        assert (
            await _exclude_dependency_commits(
                client, commits, _make_config(exclude_dependency_only=True), "test"
            )
            == commits
        )
        client.get.assert_awaited_once()
        assert client.get.call_args.kwargs["timeout"] <= 5

    def test_mise_indented_section_header_stays(self):
        from chat.changelog import _dependency_only_file

        file = dict(COLIN_PATCHES["d9efacb3104b477f60142e774f8915da993cc4ce"][0])
        file["patch"] = file["patch"].replace(' python = "3.15.1"', "   [env]")
        assert not _dependency_only_file(file)
