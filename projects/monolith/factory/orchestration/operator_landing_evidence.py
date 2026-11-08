"""Bounded immutable Git evidence for a single explicit operator approval.

This is evidence, never landing authority. The caller still verifies the owned
receipt, authenticated operator, native review, CI and queue policy. The current
base ref and the diff's merge base are distinct: PR metadata may retain an older
``base.sha``. Only hashes and metadata leave this module, never source contents.
"""

import base64
import hashlib
import json
import re
from urllib.parse import quote

REFUSAL = "operator_landing_evidence_unverified"
CHANGED = "operator_landing_evidence_changed"
MAX_FILES = 64
MAX_BLOB_BYTES = 512 * 1024
MAX_CONTENT_BYTES = 8 * 1024 * 1024
MAX_READS = 384
MAX_TREES = 256
MAX_TREE_ENTRIES = 32768
MAX_DEPTH = 32
MAX_PATH_BYTES = 1024
REGULAR_MODES = frozenset({"100644", "100755"})
_KINDS = {
    "040000": "tree",
    "100644": "blob",
    "100755": "blob",
    "120000": "blob",
    "160000": "commit",
}


def _refuse():
    raise ValueError(REFUSAL)


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        _refuse()
    return value


def _path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > MAX_PATH_BYTES
        or len(value.split("/")) > MAX_DEPTH
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(
            part in {"", ".", ".."} or part.lower() == ".git"
            for part in value.split("/")
        )
    ):
        _refuse()
    return value


def _branch(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        or value == "@"
        or value.startswith("-")
        or value.endswith(".")
        or ".." in value
        or "@{" in value
        or any(
            ord(char) <= 32 or ord(char) == 127 or char in "~^:?*[\\" for char in value
        )
        or any(
            not part or part.startswith(".") or part.endswith(".lock")
            for part in value.split("/")
        )
    ):
        _refuse()
    return value


def _get(repo, suffix):
    from factory.orchestration.factory_conductor import github_get

    return github_get(repo, suffix)


def _object_sha(kind, raw):
    return hashlib.sha1(f"{kind} {len(raw)}\0".encode("ascii") + raw).hexdigest()


def _target(pull):
    repo = pull["base"]["repo"]["full_name"]
    if (
        not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        or any(part in {".", ".."} for part in repo.split("/"))
    ):
        _refuse()
    number = pull["number"]
    count = pull["changed_files"]
    if (
        type(number) is not int
        or number < 1
        or type(count) is not int
        or not 1 <= count <= MAX_FILES
    ):
        _refuse()
    return repo, number, _branch(pull["base"]["ref"]), _sha(pull["head"]["sha"]), count


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


def _check_manifest(files):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        _refuse()
    paths = []
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "path",
                "status",
                "base_mode",
                "head_mode",
                "base_tip_mode",
                "base_blob_sha",
                "head_blob_sha",
                "base_tip_blob_sha",
                "base_content_sha256",
                "head_content_sha256",
                "base_tip_content_sha256",
            }
            or item["status"] not in {"added", "modified"}
        ):
            _refuse()
        paths.append(_path(item["path"]))
        for prefix, required in (
            ("base", item["status"] == "modified"),
            ("head", True),
            ("base_tip", False),
        ):
            mode = item[f"{prefix}_mode"]
            sha = item[f"{prefix}_blob_sha"]
            digest = item[f"{prefix}_content_sha256"]
            if mode is None:
                if required or sha is not None or digest is not None:
                    _refuse()
            elif (
                mode not in REGULAR_MODES
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                _refuse()
            else:
                _sha(sha)
        if (
            item["status"] == "added"
            and item["base_mode"] is not None
            or item["status"] == "modified"
            and item["base_mode"] == item["head_mode"]
            and item["base_blob_sha"] == item["head_blob_sha"]
        ):
            _refuse()
    if paths != sorted(set(paths)):
        _refuse()


class _Git:
    """Per-observation object cache and work bounds; mutable refs are never cached."""

    def __init__(self, repo):
        self.repo = repo
        self.reads = 0
        self.entries = 0
        self.content_bytes = 0
        self.trees = {}
        self.commits = {}
        self.blobs = {}

    def read(self, suffix):
        self.reads += 1
        if self.reads > MAX_READS:
            _refuse()
        result = _get(self.repo, suffix)
        if not isinstance(result, dict):
            _refuse()
        return result

    def tip(self, branch):
        ref = self.read(f"git/ref/heads/{quote(branch, safe='')}")
        if ref["ref"] != f"refs/heads/{branch}" or ref["object"]["type"] != "commit":
            _refuse()
        return _sha(ref["object"]["sha"])

    def root(self, revision):
        if revision not in self.commits:
            commit = self.read(f"git/commits/{revision}")
            if commit["sha"] != revision:
                _refuse()
            self.commits[revision] = _sha(commit["tree"]["sha"])
        return self.commits[revision]

    def tree(self, sha):
        if sha in self.trees:
            return self.trees[sha]
        if len(self.trees) >= MAX_TREES:
            _refuse()
        tree = self.read(f"git/trees/{sha}")
        if tree["sha"] != sha or tree.get("truncated") is not False:
            _refuse()
        rows = tree["tree"]
        if not isinstance(rows, list):
            _refuse()
        self.entries += len(rows)
        if self.entries > MAX_TREE_ENTRIES:
            _refuse()
        entries = {}
        for entry in rows:
            name = _path(entry["path"])
            if (
                "/" in name
                or name in entries
                or _KINDS.get(entry["mode"]) != entry["type"]
            ):
                _refuse()
            entries[name] = {
                "mode": entry["mode"],
                "type": entry["type"],
                "sha": _sha(entry["sha"]),
            }
        # A false/missing truncation flag cannot make a partial listing proof.
        # Git orders directory names as if they had a trailing slash.
        raw = b"".join(
            entry["mode"].lstrip("0").encode("ascii")
            + b" "
            + name.encode("utf-8")
            + b"\0"
            + bytes.fromhex(entry["sha"])
            for name, entry in sorted(
                entries.items(),
                key=lambda item: (
                    item[0] + ("/" if item[1]["type"] == "tree" else "")
                ).encode("utf-8"),
            )
        )
        if _object_sha("tree", raw) != sha:
            _refuse()
        self.trees[sha] = entries
        return entries

    def blob(self, sha):
        if sha in self.blobs:
            return self.blobs[sha]
        blob = self.read(f"git/blobs/{sha}")
        size = blob["size"]
        if (
            blob["sha"] != sha
            or blob["encoding"] != "base64"
            or type(size) is not int
            or not 0 <= size <= MAX_BLOB_BYTES
            or not isinstance(blob["content"], str)
        ):
            _refuse()
        raw = base64.b64decode("".join(blob["content"].split()), validate=True)
        if len(raw) != size or b"\0" in raw or _object_sha("blob", raw) != sha:
            _refuse()
        raw.decode("utf-8")
        self.content_bytes += len(raw)
        if self.content_bytes > MAX_CONTENT_BYTES:
            _refuse()
        digest = hashlib.sha256(raw).hexdigest()
        self.blobs[sha] = digest
        return digest

    def at(self, tree_sha, path):
        parts = path.split("/")
        for index, part in enumerate(parts):
            entry = self.tree(tree_sha).get(part)
            if entry is None:
                return None
            if index == len(parts) - 1:
                return entry
            if entry["type"] != "tree":
                _refuse()
            tree_sha = entry["sha"]
        _refuse()

    def changes(self, base_tree, head_tree):
        """Prove every changed path independently of the compare file list."""
        result = {}

        def walk(before_sha, after_sha, prefix):
            if before_sha == after_sha:
                return
            if len(prefix.split("/")) > MAX_DEPTH:
                _refuse()
            before = self.tree(before_sha) if before_sha is not None else {}
            after = self.tree(after_sha)
            for name in sorted(before.keys() | after.keys()):
                left, right = before.get(name), after.get(name)
                if left == right:
                    continue
                path = _path(prefix + name)
                if right is None:
                    _refuse()
                if right["type"] == "tree":
                    if left is not None and left["type"] != "tree":
                        _refuse()
                    walk(left["sha"] if left else None, right["sha"], path + "/")
                    continue
                if (
                    right["mode"] not in REGULAR_MODES
                    or (left is not None and left["mode"] not in REGULAR_MODES)
                    or len(result) >= MAX_FILES
                ):
                    _refuse()
                result[path] = {
                    "path": path,
                    "status": "modified" if left else "added",
                    "base_mode": left["mode"] if left else None,
                    "head_mode": right["mode"],
                    "base_blob_sha": left["sha"] if left else None,
                    "head_blob_sha": right["sha"],
                }

        walk(base_tree, head_tree, "")
        return result


def evidence(pull):
    """Return exact immutable inputs or refuse; this grants no authority.

    Only additions/modifications of regular UTF-8 files fit this bounded proof.
    The digest is SHA-256 of canonical JSON for the returned object without its
    ``manifest_sha256`` member. ``base_*`` file fields refer to the merge base;
    ``base_tip_*`` fields describe the current target branch at the same paths.
    HTTP failures and the shared GitHub cooldown remain caller-owned failures.
    """
    try:
        repo, number, branch, head, count = _target(pull)
        git = _Git(repo)
        base_tip = git.tip(branch)
        compare = git.read(f"compare/{base_tip}...{head}")
        if (
            compare["base_commit"]["sha"] != base_tip
            or compare.get("status") not in {"ahead", "diverged"}
            or compare.get("truncated", False) is not False
            or compare.get("incomplete_results", False) is not False
        ):
            _refuse()
        merge_base = _sha(compare["merge_base_commit"]["sha"])
        files = compare["files"]
        if not isinstance(files, list) or len(files) != count:
            _refuse()
        reported = {}
        for item in files:
            path = _path(item["filename"])
            if (
                path in reported
                or item["status"] not in {"added", "modified"}
                or "previous_filename" in item
            ):
                _refuse()
            reported[path] = (item["status"], _sha(item["sha"]))
        changes = git.changes(git.root(merge_base), git.root(head))
        if len(changes) != count or set(changes) != set(reported):
            _refuse()
        tip_tree = git.root(base_tip)
        manifest = []
        for path, item in sorted(changes.items()):
            if reported[path] != (item["status"], item["head_blob_sha"]):
                _refuse()
            at_tip = git.at(tip_tree, path)
            if at_tip is not None and at_tip["mode"] not in REGULAR_MODES:
                _refuse()
            base_blob, head_blob = item["base_blob_sha"], item["head_blob_sha"]
            tip_blob = at_tip["sha"] if at_tip else None
            manifest.append(
                {
                    **item,
                    "base_tip_mode": at_tip["mode"] if at_tip else None,
                    "base_tip_blob_sha": tip_blob,
                    "base_content_sha256": git.blob(base_blob) if base_blob else None,
                    "head_content_sha256": git.blob(head_blob),
                    "base_tip_content_sha256": git.blob(tip_blob) if tip_blob else None,
                }
            )
        if git.tip(branch) != base_tip:
            _refuse()
        result = {
            "schema_version": 1,
            "repo": repo,
            "pr_number": number,
            "base_ref": branch,
            "base_tip_sha": base_tip,
            "merge_base_sha": merge_base,
            "head_sha": head,
            "files": manifest,
        }
        result["manifest_sha256"] = _digest(result)
        return result
    except (AttributeError, KeyError, TypeError, UnicodeError, ValueError):
        raise ValueError(REFUSAL) from None


def confirm(pull, proof):
    """Confirm mutable inputs for an already authenticated, audited proof.

    The caller supplies a freshly observed owned PR and has already compared the
    proof byte-for-byte with its server-created audit. Immutable Git objects do
    not need another download: the same head and base-tip commits retain the
    same trees, blobs and merge base. This function rechecks the live branch ref,
    never PR metadata's historical ``base.sha``. It does not authenticate the
    audit, operator, current review or CI, and does not grant landing authority.
    """
    try:
        if (
            not isinstance(proof, dict)
            or set(proof)
            != {
                "schema_version",
                "repo",
                "pr_number",
                "base_ref",
                "base_tip_sha",
                "merge_base_sha",
                "head_sha",
                "files",
                "manifest_sha256",
            }
            or type(proof["schema_version"]) is not int
            or proof["schema_version"] != 1
            or proof["manifest_sha256"]
            != _digest({k: v for k, v in proof.items() if k != "manifest_sha256"})
        ):
            _refuse()
        _check_manifest(proof["files"])
        _sha(proof["base_tip_sha"])
        _sha(proof["merge_base_sha"])
        target = _target(pull)
        original = _target(
            {
                "base": {
                    "repo": {"full_name": proof["repo"]},
                    "ref": proof["base_ref"],
                },
                "head": {"sha": proof["head_sha"]},
                "number": proof["pr_number"],
                "changed_files": len(proof["files"]),
            }
        )
        if (
            target != original
            or _Git(proof["repo"]).tip(proof["base_ref"]) != proof["base_tip_sha"]
        ):
            raise ValueError(CHANGED)
    except (AttributeError, KeyError, TypeError, UnicodeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == CHANGED:
            raise
        raise ValueError(REFUSAL) from None
