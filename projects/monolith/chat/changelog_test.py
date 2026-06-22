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
    _CHANGELOG_TYPES,
    _auth_headers,
    _build_embed,
    _filter_changelog_commits,
    _summarize_with_qwen,
    load_changelog_configs,
    run_changelog_for_config,
    run_changelog_iteration,
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


class TestFilterChangelogCommits:
    def test_empty_list_returns_empty(self):
        """Empty input returns empty list."""
        assert _filter_changelog_commits([], _CHANGELOG_TYPES) == []

    def test_feat_commit_passes(self):
        """feat: commits are included."""
        commits = [_make_commit("feat: add dark mode")]
        assert len(_filter_changelog_commits(commits, _CHANGELOG_TYPES)) == 1

    def test_fix_commit_excluded(self):
        """fix: commits are excluded (feat-only filter)."""
        commits = [_make_commit("fix: correct typo in footer")]
        assert _filter_changelog_commits(commits, _CHANGELOG_TYPES) == []

    def test_chore_commit_excluded(self):
        """chore: commits are not included."""
        commits = [_make_commit("chore: update dependencies")]
        assert _filter_changelog_commits(commits, _CHANGELOG_TYPES) == []

    def test_docs_commit_excluded(self):
        """docs: commits are not included."""
        commits = [_make_commit("docs: improve README")]
        assert _filter_changelog_commits(commits, _CHANGELOG_TYPES) == []

    def test_refactor_commit_excluded(self):
        """refactor: commits are not included."""
        commits = [_make_commit("refactor: extract helper function")]
        assert _filter_changelog_commits(commits, _CHANGELOG_TYPES) == []

    def test_ci_commit_excluded(self):
        """ci: commits are not included."""
        commits = [_make_commit("ci: add lint step")]
        assert _filter_changelog_commits(commits, _CHANGELOG_TYPES) == []

    def test_feat_with_scope_passes(self):
        """feat(auth): scoped feature commits are included."""
        commits = [_make_commit("feat(auth): add OAuth2 support")]
        assert len(_filter_changelog_commits(commits, _CHANGELOG_TYPES)) == 1

    def test_feat_breaking_change_passes(self):
        """feat!: breaking change feature commits are included."""
        commits = [_make_commit("feat!: redesign auth token format")]
        assert len(_filter_changelog_commits(commits, _CHANGELOG_TYPES)) == 1

    def test_feat_scope_breaking_change_passes(self):
        """feat(auth)!: scoped breaking change is included."""
        commits = [_make_commit("feat(auth)!: drop legacy token support")]
        assert len(_filter_changelog_commits(commits, _CHANGELOG_TYPES)) == 1

    def test_multiline_commit_uses_first_line_only(self):
        """Only the subject line (before first newline) is matched."""
        commits = [_make_commit("feat: add feature\n\nLong description here.")]
        assert len(_filter_changelog_commits(commits, _CHANGELOG_TYPES)) == 1

    def test_multiline_commit_fix_excluded(self):
        """fix: with body is still excluded even when body mentions feat."""
        commits = [_make_commit("fix: patch crash\n\nfeat: unrelated note")]
        assert _filter_changelog_commits(commits, _CHANGELOG_TYPES) == []

    def test_mixed_commits_returns_only_feat(self):
        """Only feat commits are returned from a mixed list."""
        commits = [
            _make_commit("feat: new dashboard"),
            _make_commit("fix: remove null pointer"),
            _make_commit("chore: bump versions"),
            _make_commit("feat(ui): dark mode toggle"),
        ]
        result = _filter_changelog_commits(commits, _CHANGELOG_TYPES)
        assert len(result) == 2
        assert result[0]["commit"]["message"] == "feat: new dashboard"
        assert result[1]["commit"]["message"] == "feat(ui): dark mode toggle"

    def test_partial_word_feat_excluded(self):
        """A commit message starting with 'feature:' (not 'feat:') is excluded."""
        commits = [_make_commit("feature: add something")]
        assert _filter_changelog_commits(commits, _CHANGELOG_TYPES) == []

    def test_feat_without_colon_excluded(self):
        """'feat add something' (no colon) is excluded."""
        commits = [_make_commit("feat add something")]
        assert _filter_changelog_commits(commits, _CHANGELOG_TYPES) == []


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
# _summarize_with_qwen
# ---------------------------------------------------------------------------


class TestSummarizeWithQwen:
    @pytest.mark.asyncio
    async def test_prompt_includes_commit_messages(self):
        """The prompt forwarded to llm_call contains commit message text."""
        commits = [_make_commit("feat: add search", "Bob")]
        mock_llm = AsyncMock(return_value="Added search functionality.")

        await _summarize_with_qwen(commits, mock_llm, PROMPTS["professional"])

        call_args = mock_llm.call_args[0][0]
        assert "feat: add search" in call_args

    @pytest.mark.asyncio
    async def test_prompt_includes_author_names(self):
        """The prompt forwarded to llm_call includes the commit author names."""
        commits = [_make_commit("feat: add feature", "Charlie")]
        mock_llm = AsyncMock(return_value="Added a feature.")

        await _summarize_with_qwen(commits, mock_llm, PROMPTS["professional"])

        call_args = mock_llm.call_args[0][0]
        assert "Charlie" in call_args

    @pytest.mark.asyncio
    async def test_returns_llm_output(self):
        """Return value is exactly what the llm_call callable returns."""
        commits = [_make_commit("feat: dark mode")]
        expected = "Users can now toggle dark mode."
        mock_llm = AsyncMock(return_value=expected)

        result = await _summarize_with_qwen(commits, mock_llm, PROMPTS["professional"])

        assert result == expected

    @pytest.mark.asyncio
    async def test_llm_called_once(self):
        """llm_call is invoked exactly once per summarize call."""
        commits = [_make_commit("feat: a"), _make_commit("feat: b")]
        mock_llm = AsyncMock(return_value="summary")

        await _summarize_with_qwen(commits, mock_llm, PROMPTS["professional"])

        mock_llm.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_commits_all_appear_in_prompt(self):
        """All commit messages appear in the prompt when multiple commits are given."""
        commits = [
            _make_commit("feat: feature A", "Alice"),
            _make_commit("feat: feature B", "Bob"),
        ]
        mock_llm = AsyncMock(return_value="Two features added.")

        await _summarize_with_qwen(commits, mock_llm, PROMPTS["professional"])

        call_args = mock_llm.call_args[0][0]
        assert "feat: feature A" in call_args
        assert "feat: feature B" in call_args
        assert "Alice" in call_args
        assert "Bob" in call_args


# ---------------------------------------------------------------------------
# run_changelog_iteration
# ---------------------------------------------------------------------------


class TestRunChangelogIteration:
    @pytest.mark.asyncio
    async def test_missing_github_token_returns_early(self):
        """When GITHUB_TOKEN is absent the function exits without error."""
        bot = MagicMock(spec=discord.Client)
        mock_llm = AsyncMock()
        config = _make_config()

        with patch.dict("os.environ", {}, clear=True):
            os.environ.pop("GITHUB_TOKEN", None)
            await run_changelog_iteration(bot, mock_llm, config)

        mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_commits_returns_early(self):
        """When GitHub returns no commits, no Discord message is sent."""
        bot = MagicMock(spec=discord.Client)
        mock_llm = AsyncMock()
        config = _make_config(channel_id="111")

        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(bot, mock_llm, config)

        mock_llm.assert_not_called()
        bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_matching_commits_returns_early(self):
        """When commits exist but none match filter, no Discord message is sent."""
        bot = MagicMock(spec=discord.Client)
        mock_llm = AsyncMock()
        config = _make_config(
            channel_id="111",
            commit_filter=re.compile(r"^(feat)(\(.+?\))?!?:\s"),
        )

        commits = [
            _make_commit("fix: patch null pointer"),
            _make_commit("chore: update deps"),
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = commits
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(bot, mock_llm, config)

        mock_llm.assert_not_called()
        bot.get_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_filter_passes_all_commits(self):
        """When commit_filter is None, all commits are included."""
        mock_channel = AsyncMock()
        bot = MagicMock(spec=discord.Client)
        bot.get_channel.return_value = mock_channel
        mock_llm = AsyncMock(return_value="Summary of all commits.")
        config = _make_config(channel_id="999", commit_filter=None)

        commits = [
            _make_commit("fix: patch null pointer", "Alice"),
            _make_commit("chore: update deps", "Bob"),
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = commits
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(bot, mock_llm, config)

        mock_llm.assert_called_once()
        mock_channel.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_successful_post_sends_embed(self):
        """With valid config and matching commits, an embed is posted to Discord."""
        mock_channel = AsyncMock()
        bot = MagicMock(spec=discord.Client)
        bot.get_channel.return_value = mock_channel
        mock_llm = AsyncMock(return_value="Added a great new feature.")
        config = _make_config(
            channel_id="999",
            commit_filter=re.compile(r"^(feat)(\(.+?\))?!?:\s"),
        )

        commits = [_make_commit("feat: amazing new feature", "Alice")]
        mock_response = MagicMock()
        mock_response.json.return_value = commits
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(bot, mock_llm, config)

        bot.get_channel.assert_called_once_with(999)
        mock_channel.send.assert_called_once()
        send_kwargs = mock_channel.send.call_args[1]
        assert "embed" in send_kwargs
        assert isinstance(send_kwargs["embed"], discord.Embed)

    @pytest.mark.asyncio
    async def test_channel_not_found_does_not_raise(self):
        """When bot.get_channel returns None, no error is raised."""
        bot = MagicMock(spec=discord.Client)
        bot.get_channel.return_value = None
        mock_llm = AsyncMock(return_value="Summary text.")
        config = _make_config(channel_id="888")

        commits = [_make_commit("feat: something cool", "Dave")]
        mock_response = MagicMock()
        mock_response.json.return_value = commits
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(bot, mock_llm, config)

        bot.get_channel.assert_called_once_with(888)

    @pytest.mark.asyncio
    async def test_store_message_called_after_successful_send(self):
        """store_message callback is called with the sent message's ID and content."""
        sent_msg = MagicMock()
        sent_msg.id = 12345

        mock_channel = AsyncMock()
        mock_channel.send = AsyncMock(return_value=sent_msg)

        bot = MagicMock(spec=discord.Client)
        bot.get_channel.return_value = mock_channel
        bot.user = MagicMock()
        bot.user.id = 999
        bot.user.display_name = "Qwen3"

        mock_llm = AsyncMock(return_value="A great new feature landed.")
        store_message = AsyncMock()
        config = _make_config(channel_id="777", embed_title="Test Changelog")

        commits = [_make_commit("feat: cool thing", "Alice")]
        mock_response = MagicMock()
        mock_response.json.return_value = commits
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(
                    bot, mock_llm, config, store_message=store_message
                )

        store_message.assert_called_once()
        call_args = store_message.call_args[0]
        assert call_args[0] == "12345"  # discord_message_id
        assert call_args[1] == "777"  # channel_id
        assert call_args[2] == "999"  # user_id
        assert call_args[3] == "Qwen3"  # username
        assert "A great new feature landed." in call_args[4]  # content includes summary

    @pytest.mark.asyncio
    async def test_store_message_not_called_when_channel_not_found(self):
        """store_message is not called when the channel cannot be found."""
        bot = MagicMock(spec=discord.Client)
        bot.get_channel.return_value = None
        bot.user = MagicMock()
        bot.user.id = 999

        mock_llm = AsyncMock(return_value="Summary.")
        store_message = AsyncMock()
        config = _make_config(channel_id="777")

        commits = [_make_commit("feat: thing", "Alice")]
        mock_response = MagicMock()
        mock_response.json.return_value = commits
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(
                    bot, mock_llm, config, store_message=store_message
                )

        store_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_roast_chance_triggers_roast_prompt(self):
        """When random roll is below roast_chance, the roast prompt is used."""
        mock_channel = AsyncMock()
        bot = MagicMock(spec=discord.Client)
        bot.get_channel.return_value = mock_channel
        mock_llm = AsyncMock(return_value="Roasted.")
        config = _make_config(channel_id="999", roast_chance=1.0)

        commits = [_make_commit("feat: add thing", "Alice")]
        mock_response = MagicMock()
        mock_response.json.return_value = commits
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(bot, mock_llm, config)

        prompt_used = mock_llm.call_args[0][0]
        assert "roasting" in prompt_used

    @pytest.mark.asyncio
    async def test_roast_chance_zero_uses_normal_prompt(self):
        """When roast_chance is 0, the configured prompt is always used."""
        mock_channel = AsyncMock()
        bot = MagicMock(spec=discord.Client)
        bot.get_channel.return_value = mock_channel
        mock_llm = AsyncMock(return_value="Normal changelog.")
        config = _make_config(channel_id="999", roast_chance=0.0)

        commits = [_make_commit("feat: add thing", "Alice")]
        mock_response = MagicMock()
        mock_response.json.return_value = commits
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_tok"}):
            with patch("chat.changelog.httpx.AsyncClient", return_value=mock_client):
                await run_changelog_iteration(bot, mock_llm, config)

        prompt_used = mock_llm.call_args[0][0]
        assert "roasting" not in prompt_used
        assert "changelog writer" in prompt_used


def _configs_json(name="homelab", channel="999"):
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
