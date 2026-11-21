# invariant: ROOT-4
"""Build a clean Pages snapshot from open PRs with an approved fixture selector."""

import base64
import os
from pathlib import Path

from publish import API, REPOSITORY, SHA, pages_url, require, validate_pr

SELECTOR = "bazel/tools/previews/request.json"
FIXTURE = b'{"fixture":"mock-blog-v1"}'
PAGE = b"""<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; base-uri 'none'; form-action 'none'">
<title>Mock blog preview</title><h1>Mock blog preview</h1>
<p>Synthetic fixture for the 4090 blog layout. No measured results.</p>
"""


def selected(entry):
    if (
        not isinstance(entry, dict)
        or entry.get("type") != "file"
        or entry.get("encoding") != "base64"
    ):
        return False
    content = entry.get("content")
    if not isinstance(content, str) or len(content) > 256:
        return False
    try:
        data = base64.b64decode(content.replace("\n", ""), validate=True)
        return data.strip() == FIXTURE
    except ValueError:
        return False


def snapshot(api, destination):
    pages_url(api.repo("/pages"))
    destination.mkdir()
    (destination / "index.html").write_text("<title>Synthetic fixture previews</title>")
    for pr in api.all("/pulls?state=open&base=main"):
        number = pr["number"]
        sha = pr["head"]["sha"]
        require(isinstance(sha, str) and SHA.fullmatch(sha), "invalid PR head")
        validate_pr(pr, number, sha)
        if selected(api.optional(f"/contents/{SELECTOR}?ref={sha}")):
            stable = destination / "pr" / str(number)
            immutable = stable / sha
            immutable.mkdir(parents=True)
            for path in (stable, immutable):
                (path / "index.html").write_bytes(PAGE)


if __name__ == "__main__":
    require(os.environ["GITHUB_REPOSITORY"] == REPOSITORY, "unexpected repository")
    require(os.environ["GITHUB_REF"] == "refs/heads/main", "unexpected ref")
    snapshot(API(os.environ["GH_TOKEN"]), Path(os.environ["RUNNER_TEMP"]) / "site")
