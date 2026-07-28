"""Hourly changelog notifier — polls GitHub for new feat commits, summarizes via Qwen, posts to Discord."""

import asyncio
import dataclasses
import json
import logging
import os
import random
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import discord
import httpx

logger = logging.getLogger(__name__)

# Conventional commit types we care about
_CHANGELOG_TYPES = re.compile(r"^(feat)(\(.+?\))?!?:\s")

GITHUB_API = "https://api.github.com"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json"}


@dataclasses.dataclass(frozen=True)
class ChangelogConfig:
    name: str
    github_repo: str
    channel_id: str
    prompt: str
    embed_title: str
    embed_color: int
    interval_hours: int = 1
    commit_filter: re.Pattern | None = None
    roast_chance: float = 0.0


PROMPTS: dict[str, str] = {
    "professional": (
        "You are a changelog writer for a Kubernetes homelab project.\n"
        "Below are recent git commits (new features only).\n"
        "Write a concise changelog summarizing what changed. "
        "For each item, explain what it does in "
        "one clear sentence — don't just repeat the commit message. "
        "Use plain language. No markdown headers, just plain text with bullet points.\n\n"
        "Commits:\n{commits}"
    ),
    "roast": (
        "You are a cynical senior engineer roasting a friend's homelab commits\n"
        "in the group chat. Don't soften anything.\n\n"
        "Commits:\n<commits>\n{commits}\n</commits>\n\n"
        "Write one roast entry per commit (max 5). Each entry reads like a\n"
        "real changelog bullet, but the content is the roast. Format:\n"
        "- <entry>\n"
        "- <entry>\n\n"
        'Example: "Added three config files to manage one environment variable."\n\n'
        "Rules:\n"
        "- Past tense, declarative. No preamble, no markdown, no emoji.\n"
        "- Target specific choices in the commits — don't invent things.\n"
        '- Dry humor only. No hedging, no compliments, no "but seriously".\n'
        "- Skip genuinely boring commits. Don't manufacture heat.\n\n"
        "Optionally end with a bracketed aside, e.g.\n"
        "[No breaking changes. Nothing worked in the first place.]"
    ),
}


def load_changelog_configs(raw: str) -> list[ChangelogConfig]:
    """Parse a JSON string into a list of ChangelogConfig objects."""
    if not raw:
        return []
    entries = json.loads(raw)
    configs = []
    for entry in entries:
        commit_filter = None
        if "commitFilter" in entry:
            commit_filter = re.compile(entry["commitFilter"])
        configs.append(
            ChangelogConfig(
                name=entry["name"],
                github_repo=entry["githubRepo"],
                channel_id=entry["channelId"],
                prompt=entry["prompt"],
                embed_title=entry["embedTitle"],
                embed_color=int(entry["embedColor"], 16)
                if isinstance(entry["embedColor"], str)
                else entry["embedColor"],
                interval_hours=entry.get("intervalHours", 1),
                commit_filter=commit_filter,
                roast_chance=entry.get("roastChance", 0.0),
            )
        )
    return configs


def _auth_headers(token: str) -> dict[str, str]:
    return {**_GITHUB_HEADERS, "Authorization": f"token {token}"}


async def _fetch_commits_since(
    client: httpx.AsyncClient,
    repo: str,
    token: str,
    since: datetime,
) -> list[dict]:
    """Fetch commits on main since the given timestamp."""
    resp = await client.get(
        f"{GITHUB_API}/repos/{repo}/commits",
        params={"sha": "main", "since": since.isoformat(), "per_page": 100},
        headers=_auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


def _filter_changelog_commits(commits: list[dict], pattern: re.Pattern) -> list[dict]:
    """Keep only commits matching the given pattern."""
    result = []
    for c in commits:
        msg = c["commit"]["message"].split("\n", 1)[0]
        if pattern.match(msg):
            result.append(c)
    return result


async def _summarize_with_qwen(
    commits: list[dict],
    llm_call: Callable[[str], Awaitable[str]],
    prompt_template: str,
) -> str:
    """Ask Qwen to produce a concise changelog from commit data."""
    commit_descriptions = []
    for c in commits:
        msg = c["commit"]["message"].split("\n", 1)[0]
        author = c["commit"]["author"]["name"]
        commit_descriptions.append(f"- {msg} (by {author})")

    commits_text = "\n".join(commit_descriptions)
    prompt = prompt_template.format(commits=commits_text)
    return await llm_call(prompt)


def _build_embed(
    summary: str, commit_count: int, title: str, color: int
) -> discord.Embed:
    """Build a Discord embed for the changelog notification."""
    embed = discord.Embed(
        title=title,
        description=summary,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"{commit_count} commit(s)")
    return embed


async def run_changelog_iteration(
    bot: discord.Client,
    llm_call: Callable[[str], Awaitable[str]],
    config: ChangelogConfig,
    store_message: "Callable[[str, str, str, str, str], Awaitable[None]] | None" = None,
) -> None:
    """Single iteration: fetch recent commits, summarize, post to Discord.

    store_message, if provided, is called with (discord_message_id, channel_id,
    user_id, username, content) after a successful send so the changelog message
    is searchable in history.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "")

    if not github_token:
        logger.warning("Changelog[%s] disabled: missing GITHUB_TOKEN", config.name)
        return

    since = datetime.now(timezone.utc) - timedelta(hours=config.interval_hours)

    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        commits = await _fetch_commits_since(
            client, config.github_repo, github_token, since
        )

        if not commits:
            logger.info(
                "Changelog[%s]: no new commits in the last %dh",
                config.name,
                config.interval_hours,
            )
            return

        if config.commit_filter is not None:
            commits = _filter_changelog_commits(commits, config.commit_filter)

    if not commits:
        logger.info("Changelog[%s]: commits found but none match filter", config.name)
        return

    prompt_key = config.prompt
    if config.roast_chance > 0 and random.random() < config.roast_chance:
        prompt_key = "roast"
        logger.info("Changelog[%s]: roast mode activated", config.name)
    prompt_template = PROMPTS[prompt_key]
    summary = await _summarize_with_qwen(commits, llm_call, prompt_template)
    embed = _build_embed(
        summary, len(commits), title=config.embed_title, color=config.embed_color
    )

    channel = bot.get_channel(int(config.channel_id))
    if channel:
        sent = await channel.send(embed=embed)
        logger.info(
            "Changelog[%s]: posted %d changes to channel %s",
            config.name,
            len(commits),
            config.channel_id,
        )
        if store_message is not None and bot.user is not None:
            try:
                await store_message(
                    str(sent.id),
                    config.channel_id,
                    str(bot.user.id),
                    bot.user.display_name,
                    f"{config.embed_title}\n{summary}",
                )
            except Exception:
                logger.exception(
                    "Changelog[%s]: failed to store sent message %s",
                    config.name,
                    sent.id,
                )
    else:
        logger.warning(
            "Changelog[%s]: channel %s not found", config.name, config.channel_id
        )


def _enqueue_changelog(channel_id: str, embed_dict: dict) -> None:
    """Enqueue a changelog embed to the Discord outbox. Sync so the async job
    can hand it to a worker thread (no sync Session on the event loop)."""
    from core.db import get_engine
    from chat.outbox import enqueue_message
    from sqlmodel import Session

    with Session(get_engine()) as session:
        enqueue_message(session, channel_id, embed=embed_dict)
        session.commit()


async def run_changelog_for_config(
    name: str,
    llm_call: Callable[[str], Awaitable[str]] | None = None,
) -> None:
    """One-shot changelog for a named config, ENQUEUED to the Discord outbox.

    Module-level entrypoint for the off-pod Argo job (jobs_main chat-changelog
    <name>). It computes the embed exactly like run_changelog_iteration (GitHub
    fetch + Qwen summary), but instead of posting via the bot it enqueues the
    embed - the leader's outbox drain posts it. No bot is required, so this runs
    in an ephemeral pod. Reads the config from CHANGELOG_CONFIGS by name.

    The in-process path's best-effort store-after-send (which made the changelog
    searchable in the bot's memory, keyed on the real sent message id) is not
    reproduced here: the job has no sent id until the drain posts. Users still
    see the post; only the bot-memory searchability is dropped. It can be added
    back later by having the drain store after posting.
    """
    if llm_call is None:
        from chat.summarizer import build_llm_caller

        llm_call = build_llm_caller()

    configs = load_changelog_configs(os.environ.get("CHANGELOG_CONFIGS", ""))
    config = next((c for c in configs if c.name == name), None)
    if config is None:
        logger.warning("Changelog[%s]: no matching config in CHANGELOG_CONFIGS", name)
        return

    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        logger.warning("Changelog[%s] disabled: missing GITHUB_TOKEN", config.name)
        return

    since = datetime.now(timezone.utc) - timedelta(hours=config.interval_hours)
    async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
        commits = await _fetch_commits_since(
            client, config.github_repo, github_token, since
        )
        if not commits:
            logger.info("Changelog[%s]: no new commits", config.name)
            return
        if config.commit_filter is not None:
            commits = _filter_changelog_commits(commits, config.commit_filter)
    if not commits:
        logger.info("Changelog[%s]: commits found but none match filter", config.name)
        return

    prompt_key = config.prompt
    if config.roast_chance > 0 and random.random() < config.roast_chance:
        prompt_key = "roast"
        logger.info("Changelog[%s]: roast mode activated", config.name)
    summary = await _summarize_with_qwen(commits, llm_call, PROMPTS[prompt_key])
    embed = _build_embed(
        summary, len(commits), title=config.embed_title, color=config.embed_color
    )

    await asyncio.to_thread(_enqueue_changelog, config.channel_id, embed.to_dict())
    logger.info(
        "Changelog[%s]: enqueued %d changes to channel %s",
        config.name,
        len(commits),
        config.channel_id,
    )
