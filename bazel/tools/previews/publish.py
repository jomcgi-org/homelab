"""Trusted fixture-preview publisher. Never import or execute artifact contents.

Run only from the protected default branch. The state branch is data, not code.
The build artifact is a flat GitHub artifact ZIP of browser assets, not a tarball.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

REPOSITORY = "jomcgi-org/homelab"
BUILD_WORKFLOW = ".github/workflows/fixture-preview-build.yml"
BUILD_NAME = "Fixture preview build"
STATE_BRANCH = "fixture-preview-state"
MARKER = "fixture-preview-state-v1"
COMMENT_MARKER = "<!-- homelab-fixture-preview:v1 -->"
BOT = "github-actions[bot]"
TTL_SECONDS = 14 * 24 * 60 * 60
MAX_FILE = 10 * 1024 * 1024
MAX_ARCHIVE = 50 * 1024 * 1024
MAX_FILES = 2000
MAX_SITE = 200 * 1024 * 1024
MAX_SITE_FILES = 10000
EXTENSIONS = {
    ".html",
    ".css",
    ".js",
    ".json",
    ".txt",  # Inert font and asset license notices.
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
}
CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'none'; worker-src 'none'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
NUMBER = re.compile(r"[1-9][0-9]{0,9}\Z")


class Unsafe(ValueError):
    """A trust-boundary validation failed; publish nothing."""


class StalePreview(Unsafe):
    """A valid run is no longer the current open PR head."""


def require(condition, message):
    if not condition:
        raise Unsafe(message)


def static_path(name):
    """Restrict to portable ASCII paths; URL/Windows ambiguities are not needed."""
    require(isinstance(name, str) and 0 < len(name) <= 240, "invalid path length")
    require(re.fullmatch(r"[A-Za-z0-9_./-]+", name) is not None, "nonportable path")
    parts = name.split("/")
    require(
        all(p and p not in {".", ".."} and not p.startswith(".") for p in parts),
        "unsafe path component",
    )
    require(len(parts) <= 12, "path too deep")
    require(PurePosixPath(name).suffix.lower() in EXTENSIONS, "nonstatic file type")
    return name


class CSPParser(HTMLParser):
    """Require a real early head meta policy, preserving the tested bytes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.head = False
        self.charset = False
        self.policy = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        require(len(attrs) == len(attributes), "duplicate HTML attributes")
        if self.policy:
            return
        require(tag in {"html", "head", "meta"}, "content precedes trusted CSP")
        permitted = {
            "html": {"lang"},
            "head": set(),
            "meta": {"charset", "name", "content", "http-equiv"},
        }
        require(set(attrs) <= permitted[tag], "active attributes precede CSP")
        if tag == "head":
            require(not self.head, "duplicate head before CSP")
            self.head = True
        elif tag == "meta":
            require(self.head, "CSP metadata outside head")
            if "charset" in attrs:
                require(attrs["charset"].lower() == "utf-8", "HTML must declare UTF-8")
                self.charset = True
            equivalent = attrs.get("http-equiv", "").lower()
            require(
                equivalent in {"", "content-security-policy"},
                "unsupported early metadata",
            )
            if equivalent == "content-security-policy":
                require(self.charset, "UTF-8 declaration must precede CSP")
                # Compare the exact supported policy, rather than normalizing
                # Unicode whitespace differently from a browser's CSP parser.
                require(
                    attrs.get("content", "") == CSP, "missing or weakened fixture CSP"
                )
                self.policy = True

    def handle_endtag(self, tag):
        require(self.policy, "end tag precedes CSP")

    def handle_startendtag(self, tag, attrs):
        # HTML void metadata may use XML-style slashes. Other self-closing
        # markup before CSP has HTML5 recovery behavior we do not accept.
        require(self.policy or tag == "meta", "self-closing element precedes CSP")
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        require(
            self.policy or all(char in "\t\n\f\r " for char in data),
            "non-HTML whitespace or text precedes CSP",
        )

    def handle_comment(self, data):
        require(self.policy, "comments precede CSP")

    def handle_decl(self, decl):
        require(
            self.policy or decl.lower() == "doctype html",
            "unsupported HTML declaration",
        )

    def unknown_decl(self, data):
        require(self.policy, "unsupported HTML declaration")

    def handle_pi(self, data):
        require(self.policy, "processing instruction precedes CSP")


def validate_html(data):
    require(
        not data.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")),
        "HTML must be plain UTF-8",
    )
    parser = CSPParser()
    try:
        decoded = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise Unsafe("HTML must be UTF-8") from error
    parser.feed(decoded)
    parser.close()
    require(parser.policy, "HTML missing fixture CSP")


def extract_assets(raw):
    """Read validated ZIP members into memory without using extract/extractall."""
    require(len(raw) <= MAX_ARCHIVE, "compressed archive exceeds limit")
    assets = {}
    names = set()
    expanded = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        require(len(members) <= MAX_FILES, "too many ZIP entries")
        for member in members:
            name = member.filename
            require("\x00" not in member.orig_filename, "NUL in ZIP name")
            mode = member.external_attr >> 16
            kind = stat.S_IFMT(mode)
            require(
                kind in {0, stat.S_IFREG, stat.S_IFDIR},
                "links and special files are forbidden",
            )
            require(not member.flag_bits & 1, "encrypted ZIP entry")
            require(
                member.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED},
                "unsupported ZIP compression",
            )
            if member.is_dir():
                require(
                    kind in {0, stat.S_IFDIR} and member.file_size == 0,
                    "invalid directory",
                )
                # Validate directory syntax with an inert suffix; never create it.
                static_path(name + "directory.html")
                continue
            require(kind != stat.S_IFDIR, "directory encoded as regular file")
            static_path(name)
            require(name.casefold() not in names, "duplicate/case-colliding ZIP path")
            names.add(name.casefold())
            require(0 <= member.file_size <= MAX_FILE, "ZIP member exceeds limit")
            expanded += member.file_size
            require(expanded <= MAX_ARCHIVE, "expanded ZIP exceeds limit")
            with archive.open(member) as source:
                data = source.read(MAX_FILE + 1)
            require(
                len(data) == member.file_size and len(data) <= MAX_FILE,
                "ZIP member size mismatch",
            )
            if name.lower().endswith(".html"):
                validate_html(data)
            assets[name] = data
    require("index.html" in assets, "artifact root must contain index.html")
    for name in assets:
        require(
            not any(
                parent.as_posix() in assets for parent in PurePosixPath(name).parents
            ),
            "file/directory collision",
        )
    return assets


def validate_pr(pr, number, sha=None):
    require(pr.get("number") == number, "PR identity mismatch")
    require(
        pr.get("base", {}).get("repo", {}).get("full_name") == REPOSITORY,
        "PR base repository mismatch",
    )
    require(pr.get("base", {}).get("ref") == "main", "PR does not target main")
    if sha is not None and (
        pr.get("state") != "open" or pr.get("head", {}).get("sha") != sha
    ):
        raise StalePreview("closed PR or stale head")


def validate_run(run, workflow, event_run, pr):
    """All identity facts come from authenticated API responses and the event."""
    require(run.get("id") == event_run.get("id"), "run identity mismatch")
    require(
        run.get("run_attempt") == event_run.get("run_attempt"), "superseded run attempt"
    )
    require(
        run.get("repository", {}).get("full_name") == REPOSITORY,
        "run repository mismatch",
    )
    require(run.get("event") == "pull_request", "unexpected build trigger")
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "build did not succeed",
    )
    require(
        workflow.get("path") == BUILD_WORKFLOW and workflow.get("name") == BUILD_NAME,
        "workflow identity mismatch",
    )
    require(
        run.get("workflow_id") == workflow.get("id")
        and run.get("path", "").partition("@")[0] == BUILD_WORKFLOW,
        "run workflow mismatch",
    )
    sha = run.get("head_sha", "")
    require(SHA.fullmatch(sha) is not None, "invalid head SHA")
    associated = run.get("pull_requests", [])
    require(
        len(associated) == 1 and associated[0].get("number") == pr.get("number"),
        "ambiguous run PR association",
    )
    require(associated[0].get("head", {}).get("sha") == sha, "run PR head mismatch")
    validate_pr(pr, pr["number"], sha)
    require(
        run.get("head_repository", {}).get("full_name")
        == pr.get("head", {}).get("repo", {}).get("full_name"),
        "head repository mismatch",
    )
    return pr["number"], sha


def pages_url(config):
    require(
        config.get("build_type") == "workflow", "Pages must already use GitHub Actions"
    )
    require(
        not config.get("cname"), "custom-domain Pages is not a fixture preview origin"
    )
    url = config.get("html_url", "")
    parsed = urllib.parse.urlsplit(url)
    owner, repo = REPOSITORY.split("/")
    require(
        parsed.scheme == "https" and parsed.netloc == f"{owner}.github.io",
        "unexpected Pages origin",
    )
    require(
        parsed.path.rstrip("/") == f"/{repo}"
        and not parsed.query
        and not parsed.fragment,
        "unexpected Pages path",
    )
    return url.rstrip("/") + "/"


def empty_state(url):
    return {
        "schema": MARKER,
        "repository": REPOSITORY,
        "pages_url": url,
        "previews": {},
    }


def validate_state(state, url):
    require(
        state.get("schema") == MARKER and state.get("repository") == REPOSITORY,
        "unrecognized state branch; refusing to replace it",
    )
    require(state.get("pages_url") == url, "Pages URL differs from owned state")
    require(isinstance(state.get("previews"), dict), "invalid preview state")
    for number, entry in state["previews"].items():
        require(NUMBER.fullmatch(number) is not None, "invalid state PR")
        require(
            isinstance(entry, dict) and SHA.fullmatch(entry.get("sha", "")) is not None,
            "invalid state SHA",
        )
        require(
            type(entry.get("updated_at")) is int and entry["updated_at"] > 0,
            "invalid state timestamp",
        )
    return state


def reconcile(state, files, current_prs, now):
    """Remove only expired or closed PRs; retain each open PR latest passing build."""
    for number, entry in list(state["previews"].items()):
        pr = current_prs[number]
        validate_pr(pr, int(number))
        if pr.get("state") != "open" or now - entry["updated_at"] >= TTL_SECONDS:
            prefix = f"site/pr/{number}/"
            for path in list(files):
                if path.startswith(prefix):
                    del files[path]
            del state["previews"][number]
    return state, files


def trusted_html_head(title):
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{title}</title>"
    )


def add_preview(state, files, number, sha, assets, now, run_id):
    require(
        NUMBER.fullmatch(str(number)) is not None and SHA.fullmatch(sha) is not None,
        "invalid preview identity",
    )
    prefix = f"site/pr/{number}/"
    old = state["previews"].get(str(number))
    # Re-runs cannot silently replace a supposedly immutable SHA URL.
    if old and old["sha"] == sha:
        existing = {
            path[len(prefix + sha + "/") :]: data
            for path, data in files.items()
            if path.startswith(prefix + sha + "/")
        }
        require(
            existing == assets,
            "same-SHA artifact differs; immutable preview cannot be replaced",
        )
    for path in list(files):
        if path.startswith(prefix):
            del files[path]
    for path, data in assets.items():
        files[f"{prefix}{sha}/{static_path(path)}"] = data
    target = f"./{sha}/"
    files[prefix + "index.html"] = (
        trusted_html_head(f"Fixture preview #{number}")
        + f'<meta http-equiv=refresh content="0;url={target}"></head><body>'
        + f'<a href="{target}">Open synthetic fixture preview</a></body></html>'
    ).encode()
    state["previews"][str(number)] = {"sha": sha, "updated_at": now, "run_id": run_id}


def finish_site(state, files):
    links = "".join(
        f'<li><a href="pr/{number}/">PR #{number}</a> <code>{entry["sha"][:12]}</code></li>'
        for number, entry in sorted(
            state["previews"].items(), key=lambda pair: int(pair[0])
        )
    )
    files["site/index.html"] = (
        trusted_html_head("Synthetic fixture previews")
        + "</head><body><h1>Synthetic fixture previews</h1>"
        + "<p>Public, untrusted PR code. Never enter credentials or personal data.</p>"
        + f"<ul>{links}</ul></body></html>"
    ).encode()
    files["site/.nojekyll"] = b""
    files["state.json"] = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode()
    require(
        len(files) <= MAX_SITE_FILES and sum(map(len, files.values())) <= MAX_SITE,
        "aggregate site exceeds limit",
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, path, method="GET", payload=None, limit=MAX_SITE):
        require(path.startswith("/repos/"), "unexpected API path")
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.github.com" + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "homelab-fixture-publisher",
            },
        )
        with self.opener.open(req, timeout=60) as response:
            raw = response.read(limit + 1)
        require(len(raw) <= limit, "API response exceeds limit")
        return json.loads(raw) if raw else None

    def repo(self, path, **kwargs):
        return self.request(f"/repos/{REPOSITORY}{path}", **kwargs)

    def optional(self, path):
        try:
            return self.repo(path)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def all(self, path, key=None):
        result = []
        for page in range(1, 101):
            data = self.repo(
                f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}"
            )
            items = data[key] if key else data
            result.extend(items)
            if len(items) < 100:
                return result
        raise Unsafe("pagination exceeds limit")

    def artifact(self, artifact):
        require(not artifact.get("expired"), "artifact expired")
        require(
            0 < artifact.get("size_in_bytes", 0) <= MAX_ARCHIVE,
            "artifact size exceeds limit",
        )
        digest = artifact.get("digest", "")
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
            "missing artifact digest",
        )
        # GitHub's signed storage redirect is followed without the GitHub token.
        try:
            self.repo(f"/actions/artifacts/{int(artifact['id'])}/zip")
            raise Unsafe("artifact endpoint did not provide a signed redirect")
        except urllib.error.HTTPError as error:
            require(error.code == 302, "unexpected artifact response")
            location = error.headers["Location"]
        parsed = urllib.parse.urlsplit(location)
        require(
            parsed.scheme == "https"
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password,
            "unsafe artifact redirect",
        )
        require(
            parsed.hostname
            and parsed.hostname.endswith(
                (".blob.core.windows.net", ".githubusercontent.com")
            ),
            "unexpected artifact storage host",
        )
        with self.opener.open(urllib.request.Request(location), timeout=60) as response:
            raw = response.read(MAX_ARCHIVE + 1)
        require(
            len(raw) <= MAX_ARCHIVE
            and "sha256:" + hashlib.sha256(raw).hexdigest() == digest,
            "artifact size/digest mismatch",
        )
        return raw


def state_path(path):
    if path in {"state.json", "site/.nojekyll", "site/index.html"}:
        return path
    require(
        re.fullmatch(r"site/pr/[1-9][0-9]{0,9}/(?:index.html|[0-9a-f]{40}/.+)", path)
        is not None,
        "unknown file in state branch",
    )
    static_path(path.split("/", 4)[4] if path.count("/") >= 4 else "index.html")
    return path


def load_state(api, url, bootstrap):
    ref = api.optional(f"/git/ref/heads/{STATE_BRANCH}")
    if ref is None:
        require(
            bootstrap,
            "state branch missing; dedicated Pages ownership must be explicitly acknowledged before bootstrap",
        )
        return None, empty_state(url), {}
    commit_sha = ref["object"]["sha"]
    require(SHA.fullmatch(commit_sha) is not None, "invalid state commit")
    commit = api.repo(f"/git/commits/{commit_sha}")
    tree = api.repo(f"/git/trees/{commit['tree']['sha']}?recursive=1")
    require(not tree.get("truncated"), "state tree truncated")
    entries = [entry for entry in tree["tree"] if entry["type"] != "tree"]
    require(len(entries) <= MAX_SITE_FILES, "state has too many files")
    files = {}
    total = 0
    for entry in entries:
        require(
            entry["type"] == "blob" and entry["mode"] == "100644",
            "state contains executable, link or special file",
        )
        path = state_path(entry["path"])
        require(
            0 <= entry.get("size", MAX_FILE + 1) <= MAX_FILE, "state file exceeds limit"
        )
        total += entry["size"]
        require(total <= MAX_SITE, "state exceeds size limit")
        blob = api.repo(f"/git/blobs/{entry['sha']}")
        require(blob.get("encoding") == "base64", "unexpected blob encoding")
        raw = base64.b64decode(blob["content"], validate=False)
        require(blob_sha(raw) == entry["sha"], "state blob hash mismatch")
        require(len(raw) == entry["size"], "state blob size mismatch")
        files[path] = raw
    require("state.json" in files, "unmarked state branch; refusing replacement")
    state = validate_state(json.loads(files["state.json"]), url)
    expected_prefixes = {
        f"site/pr/{number}/{entry['sha']}/"
        for number, entry in state["previews"].items()
    }
    for path in files:
        if path.startswith("site/pr/"):
            require(path.split("/")[2] in state["previews"], "orphan state pointer")
            if not re.fullmatch(r"site/pr/[1-9][0-9]{0,9}/index.html", path):
                require(
                    any(path.startswith(prefix) for prefix in expected_prefixes),
                    "orphan state assets",
                )
    for number, entry in state["previews"].items():
        require(
            f"site/pr/{number}/{entry['sha']}/index.html" in files,
            "incomplete stored preview",
        )
    return commit_sha, state, files


def blob_sha(data):
    return hashlib.sha1(
        f"blob {len(data)}\0".encode() + data, usedforsecurity=False
    ).hexdigest()


def save_state(api, parent, files, known_blobs):
    # A new full tree preserves exactly the validated aggregate, never unknown data.
    tree = []
    for path, data in sorted(files.items()):
        sha = blob_sha(data)
        if sha not in known_blobs:
            blob = api.repo(
                "/git/blobs",
                method="POST",
                payload={
                    "encoding": "base64",
                    "content": base64.b64encode(data).decode(),
                },
            )
            require(blob["sha"] == sha, "stored blob hash mismatch")
        tree.append({"path": path, "type": "blob", "mode": "100644", "sha": sha})
    result = api.repo("/git/trees", method="POST", payload={"tree": tree})
    commit = api.repo(
        "/git/commits",
        method="POST",
        payload={
            "message": "chore(previews): reconcile synthetic fixture previews",
            "tree": result["sha"],
            "parents": [parent] if parent else [],
        },
    )
    if parent:
        api.repo(
            f"/git/refs/heads/{STATE_BRANCH}",
            method="PATCH",
            payload={"sha": commit["sha"], "force": False},
        )
    else:
        api.repo(
            "/git/refs",
            method="POST",
            payload={"ref": f"refs/heads/{STATE_BRANCH}", "sha": commit["sha"]},
        )
    return commit["sha"]


def current_prs(api, state):
    return {number: api.repo(f"/pulls/{number}") for number in state["previews"]}


def output(name, value):
    require("\n" not in str(value), "multiline output")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as destination:
        destination.write(f"{name}={value}\n")


def prepare(api, event, event_name, directory, bootstrap):
    require(
        event.get("repository", {}).get("full_name") == REPOSITORY,
        "event repository mismatch",
    )
    url = pages_url(api.repo("/pages"))
    candidate = None
    if event_name == "workflow_run":
        event_run = event["workflow_run"]
        require(type(event_run.get("id")) is int, "invalid event run id")
        run = api.repo(f"/actions/runs/{event_run['id']}")
        workflow = api.repo(
            f"/actions/workflows/{urllib.parse.quote(BUILD_WORKFLOW.rsplit('/', 1)[1])}"
        )
        associations = run.get("pull_requests", [])
        require(
            len(associations) == 1 and type(associations[0].get("number")) is int,
            "missing/untrusted PR association",
        )
        pr = api.repo(f"/pulls/{associations[0]['number']}")
        try:
            number, sha = validate_run(run, workflow, event_run, pr)
        except StalePreview:
            print("Ignoring a closed/superseded build; reconciling existing previews.")
        else:
            artifacts = api.all(f"/actions/runs/{run['id']}/artifacts", "artifacts")
            matching = [
                a for a in artifacts if a.get("name") == f"fixture-preview-{sha}"
            ]
            require(len(matching) == 1, "expected one exact-SHA artifact")
            artifact = matching[0]
            require(
                artifact.get("workflow_run", {}).get("id") == run["id"]
                and artifact["workflow_run"].get("head_sha") == sha,
                "artifact run provenance mismatch",
            )
            candidate = (number, sha, extract_assets(api.artifact(artifact)), run["id"])
    elif event_name == "pull_request_target":
        require(
            event.get("action") == "closed",
            "only closed pull_request_target events are supported",
        )
        validate_pr(api.repo(f"/pulls/{int(event['number'])}"), event["number"])
    else:
        require(event_name in {"schedule", "workflow_dispatch"}, "unsupported event")
    parent, state, files = load_state(api, url, bootstrap)
    known_blobs = {blob_sha(data) for data in files.values()}
    now = int(time.time())
    reconcile(state, files, current_prs(api, state), now)
    if candidate:
        number, sha, assets, run_id = candidate
        # A PR may have changed while its archive was being downloaded.
        try:
            validate_pr(api.repo(f"/pulls/{number}"), number, sha)
        except StalePreview:
            candidate = None
        else:
            add_preview(state, files, number, sha, assets, now, run_id)
    finish_site(state, files)
    commit = save_state(api, parent, files, known_blobs)
    destination = Path(directory)
    require(not destination.exists(), "output directory must not already exist")
    destination.mkdir(parents=True)
    for path, data in files.items():
        if path.startswith("site/"):
            target = destination / path.removeprefix("site/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    output("state_commit", commit)
    output("pages_url", url)
    output("pr", candidate[0] if candidate else "")
    output("sha", candidate[1] if candidate else "")
    output("ready", "true")


def guard(api, expected_commit, candidate_number=None, candidate_sha=None):
    url = pages_url(api.repo("/pages"))
    require(SHA.fullmatch(expected_commit) is not None, "invalid expected state commit")
    ref = api.repo(f"/git/ref/heads/{STATE_BRANCH}")
    require(ref["object"]["sha"] == expected_commit, "state changed before deployment")
    stored = api.repo(f"/contents/state.json?ref={expected_commit}")
    require(
        stored.get("type") == "file" and stored.get("encoding") == "base64",
        "invalid stored state",
    )
    state = validate_state(json.loads(base64.b64decode(stored["content"])), url)
    now = int(time.time())
    for number, entry in state["previews"].items():
        pull = api.repo(f"/pulls/{number}")
        validate_pr(pull, int(number))
        require(pull.get("state") == "open", "PR closed before deployment")
        require(
            now - entry["updated_at"] < TTL_SECONDS, "preview expired before deployment"
        )
        if number == candidate_number:
            require(
                entry["sha"] == candidate_sha, "candidate differs from aggregate state"
            )
            validate_pr(pull, int(number), candidate_sha)
    if candidate_number:
        require(
            candidate_number in state["previews"],
            "candidate missing from aggregate state",
        )
    return url


def comment(api, number, sha, deployed_url):
    url = pages_url(api.repo("/pages"))
    require(
        deployed_url.rstrip("/") + "/" == url,
        "deployment URL differs from verified Pages URL",
    )
    validate_pr(api.repo(f"/pulls/{number}"), number, sha)
    stable = urllib.parse.urljoin(url, f"pr/{number}/")
    immutable = urllib.parse.urljoin(url, f"pr/{number}/{sha}/")
    body = (
        f"{COMMENT_MARKER}\nSynthetic fixture preview: [open preview]({stable})\n\n"
        f"Commit `{sha}`: [immutable URL]({immutable})\n\n"
        "Public synthetic fixtures only. This runs untrusted PR JavaScript; do not enter credentials or personal data. "
        "This is the latest passing preview; the PR may have newer untested changes. Removed on close or 14 days after the last successful build, on the next reconciliation. A new passing build replaces this SHA URL."
    )
    comments = api.all(f"/issues/{number}/comments")
    ours = [
        c
        for c in comments
        if c.get("user", {}).get("login") == BOT
        and c.get("user", {}).get("type") == "Bot"
        and c.get("body", "").startswith(COMMENT_MARKER)
    ]
    require(len(ours) <= 1, "multiple preview bot comments; refusing ambiguous update")
    if ours:
        api.repo(
            f"/issues/comments/{ours[0]['id']}", method="PATCH", payload={"body": body}
        )
    else:
        api.repo(f"/issues/{number}/comments", method="POST", payload={"body": body})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "guard", "comment"])
    parser.add_argument("--output", default="preview-site")
    args = parser.parse_args()
    require(
        os.environ.get("GITHUB_REPOSITORY") == REPOSITORY,
        "publisher is repository-scoped",
    )
    api = API(os.environ["GH_TOKEN"])
    if args.command == "prepare":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        prepare(
            api,
            event,
            os.environ["GITHUB_EVENT_NAME"],
            args.output,
            os.environ.get("BOOTSTRAP_OWNED_SITE") == "true",
        )
    elif args.command == "guard":
        guard(
            api,
            os.environ["STATE_COMMIT"],
            os.environ.get("PREVIEW_PR"),
            os.environ.get("PREVIEW_SHA"),
        )
        output("ready", "true")
    else:
        number, sha = os.environ["PREVIEW_PR"], os.environ["PREVIEW_SHA"]
        require(
            NUMBER.fullmatch(number) is not None and SHA.fullmatch(sha) is not None,
            "invalid comment identity",
        )
        comment(api, int(number), sha, os.environ["DEPLOYED_URL"])


if __name__ == "__main__":
    try:
        main()
    except (Unsafe, urllib.error.HTTPError, zipfile.BadZipFile) as error:
        # Do not print signed artifact URLs, headers or bodies with credentials.
        print(
            f"Fixture preview stopped: {type(error).__name__}: {error if isinstance(error, Unsafe) else 'remote request or artifact validation failed'}",
            file=sys.stderr,
        )
        sys.exit(1)
