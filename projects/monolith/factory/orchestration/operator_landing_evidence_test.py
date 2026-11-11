"""Immutable diff completeness and mutable-base fencing for operator approval."""

import base64
import copy
import hashlib
import json

import pytest

from factory.orchestration import operator_landing_evidence as mod

REPO = "owner/repo"
BASE = "1" * 40
HEAD = "2" * 40
TIP = "3" * 40
STALE = "4" * 40
BRANCH = "release/next"
REF = "git/ref/heads/release%2Fnext"
COMPARE = f"compare/{TIP}...{HEAD}"


def git_hash(kind, raw):
    return hashlib.sha1(
        kind.encode() + b" " + str(len(raw)).encode() + b"\0" + raw
    ).hexdigest()


def proof_hash(value):
    return hashlib.sha256(
        json.dumps(
            {key: item for key, item in value.items() if key != "manifest_sha256"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


class Repository:
    """Small content-addressed fixture with actual Git tree and blob hashes."""

    def __init__(self):
        self.data = {}
        self.calls = []
        self.before = {
            "src/a.py": ("100644", b"old\n"),
            "run.sh": ("100644", b"echo same\n"),
            "same/kept.txt": ("100644", b"unchanged\n"),
            "same/link": ("120000", b"kept.txt"),
        }
        self.after = {
            **self.before,
            "src/a.py": ("100644", b"new\n"),
            "src/new.py": ("100644", b"added\n"),
            "run.sh": ("100755", b"echo same\n"),
        }
        self.tip = {
            **self.before,
            "src/a.py": ("100644", b"independent main change\n"),
            "main-only.txt": ("100644", b"unrelated main change\n"),
        }
        self.rebuild()

    def blob(self, raw):
        sha = git_hash("blob", raw)
        self.data[f"git/blobs/{sha}"] = {
            "sha": sha,
            "size": len(raw),
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
        }
        return sha

    def tree(self, snapshot):
        nested = {}
        for path, (mode, raw) in snapshot.items():
            parent = nested
            parts = path.split("/")
            for part in parts[:-1]:
                parent = parent.setdefault(part, {})
            kind = "commit" if mode == "160000" else "blob"
            parent[parts[-1]] = (
                mode,
                kind,
                raw if kind == "commit" else self.blob(raw),
            )

        def build(directory):
            rows = []
            for name, child in directory.items():
                if isinstance(child, dict):
                    rows.append(
                        {
                            "path": name,
                            "mode": "040000",
                            "type": "tree",
                            "sha": build(child),
                        }
                    )
                else:
                    mode, kind, sha = child
                    rows.append({"path": name, "mode": mode, "type": kind, "sha": sha})
            rows.sort(
                key=lambda row: (
                    row["path"] + ("/" if row["type"] == "tree" else "")
                ).encode()
            )
            body = b"".join(
                f"{int(row['mode'])} {row['path']}".encode()
                + b"\0"
                + bytes.fromhex(row["sha"])
                for row in rows
            )
            sha = git_hash("tree", body)
            self.data[f"git/trees/{sha}"] = {
                "sha": sha,
                "truncated": False,
                "tree": rows,
            }
            return sha

        return build(nested)

    def rebuild(self):
        self.data = {}
        roots = [
            self.tree(snapshot) for snapshot in (self.before, self.after, self.tip)
        ]
        for commit, tree in zip((BASE, HEAD, TIP), roots, strict=True):
            self.data[f"git/commits/{commit}"] = {"sha": commit, "tree": {"sha": tree}}
        files = []
        for path in self.before.keys() | self.after.keys():
            before, after = self.before.get(path), self.after.get(path)
            if before == after:
                continue
            status = (
                "removed"
                if after is None
                else "added"
                if before is None
                else "modified"
            )
            mode, raw = after if after else before
            sha = raw if mode == "160000" else git_hash("blob", raw)
            files.append({"filename": path, "status": status, "sha": sha})
        self.data[COMPARE] = {
            "base_commit": {"sha": TIP},
            "merge_base_commit": {"sha": BASE},
            "status": "diverged",
            "files": files,
        }
        self.data[REF] = {
            "ref": f"refs/heads/{BRANCH}",
            "object": {"type": "commit", "sha": TIP},
        }
        self.pull = {
            "number": 7,
            "base": {"repo": {"full_name": REPO}, "ref": BRANCH, "sha": STALE},
            "head": {"sha": HEAD},
            "changed_files": len(files),
        }

    def get(self, repo, suffix):
        assert repo == REPO
        self.calls.append(suffix)
        return copy.deepcopy(self.data[suffix])

    def root_tree(self, commit):
        return self.data[
            f"git/trees/{self.data[f'git/commits/{commit}']['tree']['sha']}"
        ]


@pytest.fixture
def repository(monkeypatch):
    value = Repository()
    monkeypatch.setattr(mod, "_get", value.get)
    return value


def rejected(repository):
    with pytest.raises(ValueError, match=f"^{mod.REFUSAL}$"):
        mod.evidence(repository.pull)


def test_complete_diff_distinguishes_merge_base_and_current_target(repository):
    proof = mod.evidence(repository.pull)
    assert proof["base_tip_sha"] == TIP
    assert proof["merge_base_sha"] == BASE
    assert proof["head_sha"] == HEAD
    assert proof["base_ref"] == BRANCH
    assert proof["repo"] == REPO and proof["pr_number"] == 7
    assert proof["schema_version"] == 1
    assert proof["manifest_sha256"] == proof_hash(proof)
    files = {item["path"]: item for item in proof["files"]}
    assert list(files) == ["run.sh", "src/a.py", "src/new.py"]
    changed = files["src/a.py"]
    assert changed["base_blob_sha"] == git_hash("blob", b"old\n")
    assert changed["head_blob_sha"] == git_hash("blob", b"new\n")
    assert changed["base_tip_blob_sha"] == git_hash(
        "blob", b"independent main change\n"
    )
    assert changed["base_content_sha256"] == hashlib.sha256(b"old\n").hexdigest()
    assert changed["head_content_sha256"] == hashlib.sha256(b"new\n").hexdigest()
    assert (
        changed["base_tip_content_sha256"]
        == hashlib.sha256(b"independent main change\n").hexdigest()
    )
    added = files["src/new.py"]
    assert added["status"] == "added"
    assert all(
        added[key] is None
        for key in (
            "base_blob_sha",
            "base_mode",
            "base_content_sha256",
            "base_tip_blob_sha",
            "base_tip_mode",
            "base_tip_content_sha256",
        )
    )
    executable = files["run.sh"]
    assert executable["base_mode"] == "100644" and executable["head_mode"] == "100755"
    assert executable["base_blob_sha"] == executable["head_blob_sha"]
    assert repository.calls.count(REF) == 2
    assert all(
        repository.calls.count(path) == 1 for path in repository.calls if path != REF
    )
    assert "old\\n" not in json.dumps(proof)
    assert "independent main change" not in json.dumps(proof)
    assert STALE not in json.dumps(proof)


def test_file_order_and_whitespace_in_base64_do_not_change_proof(repository):
    first = mod.evidence(repository.pull)
    repository.data[COMPARE]["files"].reverse()
    for path, value in repository.data.items():
        if path.startswith("git/blobs/"):
            value["content"] = "\n " + value["content"] + "\n"
    assert mod.evidence(repository.pull) == first


def test_empty_added_file_is_valid(repository):
    repository.after["src/empty.py"] = ("100644", b"")
    repository.rebuild()
    files = {item["path"]: item for item in mod.evidence(repository.pull)["files"]}
    assert (
        files["src/empty.py"]["head_blob_sha"]
        == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    )
    assert (
        files["src/empty.py"]["head_content_sha256"] == hashlib.sha256(b"").hexdigest()
    )


def test_full_64_path_bound_and_one_more_refuses(repository):
    repository.before = {}
    repository.tip = {}
    repository.after = {
        f"src/{index:02}.py": ("100644", b"text\n") for index in range(64)
    }
    repository.rebuild()
    assert len(mod.evidence(repository.pull)["files"]) == 64
    repository.after["one-more.py"] = ("100644", b"text\n")
    repository.rebuild()
    rejected(repository)


@pytest.mark.parametrize(
    "fault",
    [
        "missing_file",
        "duplicate",
        "wrong_blob",
        "wrong_status",
        "rename",
        "removed",
        "unsafe_path",
        "incomplete_flag",
        "truncated_flag",
        "wrong_base",
        "invalid_merge_base",
    ],
)
def test_compare_metadata_never_substitutes_for_complete_tree_proof(repository, fault):
    compare = repository.data[COMPARE]
    if fault == "missing_file":
        compare["files"].pop()
        repository.pull["changed_files"] -= 1
    elif fault == "duplicate":
        compare["files"][0] = copy.deepcopy(compare["files"][1])
    elif fault == "wrong_blob":
        compare["files"][0]["sha"] = "a" * 40
    elif fault == "wrong_status":
        item = next(item for item in compare["files"] if item["status"] == "modified")
        item["status"] = "added"
    elif fault == "rename":
        compare["files"][0]["previous_filename"] = None
    elif fault == "removed":
        compare["files"][0]["status"] = "removed"
    elif fault == "unsafe_path":
        compare["files"][0]["filename"] = "src/../secret"
    elif fault == "incomplete_flag":
        compare["incomplete_results"] = True
    elif fault == "truncated_flag":
        compare["truncated"] = True
    elif fault == "wrong_base":
        compare["base_commit"]["sha"] = STALE
    elif fault == "invalid_merge_base":
        compare["merge_base_commit"]["sha"] = "not-a-sha"
    rejected(repository)


@pytest.mark.parametrize(
    "fault",
    [
        "short",
        "duplicate",
        "truncated",
        "missing_flag",
        "wrong_sha",
        "wrong_commit",
        "malformed_entry",
    ],
)
def test_tree_hash_requires_complete_exact_tree(repository, fault):
    tree = repository.root_tree(HEAD)
    if fault == "short":
        tree["tree"].pop()
    elif fault == "duplicate":
        tree["tree"].append(copy.deepcopy(tree["tree"][0]))
    elif fault == "truncated":
        tree["truncated"] = True
    elif fault == "missing_flag":
        del tree["truncated"]
    elif fault == "wrong_sha":
        tree["sha"] = STALE
    elif fault == "wrong_commit":
        repository.data[f"git/commits/{HEAD}"]["sha"] = STALE
    elif fault == "malformed_entry":
        tree["tree"][0]["type"] = None
    rejected(repository)


@pytest.mark.parametrize(
    "fault",
    [
        "symlink",
        "submodule",
        "deleted_file",
        "deleted_directory",
        "file_to_directory",
        "directory_to_file",
        "tip_symlink",
    ],
)
def test_unsupported_changed_file_kinds_and_deletions_refuse(repository, fault):
    if fault == "symlink":
        repository.after["src/a.py"] = ("120000", b"new-target")
    elif fault == "submodule":
        repository.after["src/a.py"] = ("160000", "f" * 40)
    elif fault == "deleted_file":
        del repository.after["run.sh"]
    elif fault == "deleted_directory":
        del repository.after["same/kept.txt"]
        del repository.after["same/link"]
    elif fault == "file_to_directory":
        del repository.after["run.sh"]
        repository.after["run.sh/a.py"] = ("100644", b"new\n")
    elif fault == "directory_to_file":
        del repository.after["same/kept.txt"]
        del repository.after["same/link"]
        repository.after["same"] = ("100644", b"new\n")
    elif fault == "tip_symlink":
        repository.tip["src/a.py"] = ("120000", b"new-target")
    repository.rebuild()
    rejected(repository)


@pytest.mark.parametrize(
    "fault",
    [
        "bad_sha",
        "bad_encoding",
        "bad_content",
        "wrong_size",
        "bool_size",
        "false_contents",
    ],
)
def test_blob_response_cannot_lie_about_bound_bytes(repository, fault):
    sha = git_hash("blob", b"new\n")
    blob = repository.data[f"git/blobs/{sha}"]
    if fault == "bad_sha":
        blob["sha"] = STALE
    elif fault == "bad_encoding":
        blob["encoding"] = "utf-8"
    elif fault == "bad_content":
        blob["content"] = "not!base64"
    elif fault == "wrong_size":
        blob["size"] += 1
    elif fault == "bool_size":
        blob["size"] = True
    elif fault == "false_contents":
        blob["content"] = base64.b64encode(b"bad\n").decode()
    rejected(repository)


@pytest.mark.parametrize("raw", [b"nul\0byte", b"invalid\xff", b"x" * (512 * 1024 + 1)])
def test_binary_non_utf8_and_oversize_blobs_refuse(repository, raw):
    repository.after["src/a.py"] = ("100644", raw)
    repository.rebuild()
    rejected(repository)


@pytest.mark.parametrize(
    "limit", ["MAX_READS", "MAX_TREES", "MAX_TREE_ENTRIES", "MAX_CONTENT_BYTES"]
)
def test_bounded_observation_work_refuses_incomplete_proof(
    repository, monkeypatch, limit
):
    monkeypatch.setattr(mod, limit, 1)
    rejected(repository)


@pytest.mark.parametrize("path", ["number", "changed_files", "repo", "branch", "head"])
def test_malformed_target_refuses_before_observing(repository, path):
    if path == "number":
        repository.pull["number"] = True
    elif path == "changed_files":
        repository.pull["changed_files"] = True
    elif path == "repo":
        repository.pull["base"]["repo"]["full_name"] = "../repo"
    elif path == "branch":
        repository.pull["base"]["ref"] = "main?query"
    elif path == "head":
        repository.pull["head"]["sha"] = "A" * 40
    rejected(repository)
    assert not repository.calls


@pytest.mark.parametrize(
    "fault", ["wrong_ref", "wrong_type", "missing_sha", "missing_object"]
)
def test_base_ref_must_prove_exact_branch_commit(repository, fault):
    ref = repository.data[REF]
    if fault == "wrong_ref":
        ref["ref"] = "refs/heads/not-the-target"
    elif fault == "wrong_type":
        ref["object"]["type"] = "tag"
    elif fault == "missing_sha":
        del ref["object"]["sha"]
    elif fault == "missing_object":
        del ref["object"]
    rejected(repository)


def test_base_movement_during_full_observation_refuses(repository, monkeypatch):
    def read(repo, suffix):
        result = repository.get(repo, suffix)
        if suffix == REF and repository.calls.count(REF) == 2:
            result["object"]["sha"] = STALE
        return result

    monkeypatch.setattr(mod, "_get", read)
    rejected(repository)


def test_confirm_rechecks_mutable_ref_without_redownloading_immutable_objects(
    repository,
):
    proof = mod.evidence(repository.pull)
    repository.calls.clear()
    del repository.pull["base"]["sha"]
    assert mod.confirm(repository.pull, proof) is None
    assert repository.calls == [REF]


@pytest.mark.parametrize(
    "field", ["repo", "number", "base_ref", "head_sha", "changed_files", "base_tip"]
)
def test_confirm_refuses_changed_identity_or_integration_base(repository, field):
    proof = mod.evidence(repository.pull)
    repository.calls.clear()
    if field == "repo":
        repository.pull["base"]["repo"]["full_name"] = "owner/other"
    elif field == "number":
        repository.pull["number"] = 8
    elif field == "base_ref":
        repository.pull["base"]["ref"] = "main"
    elif field == "head_sha":
        repository.pull["head"]["sha"] = STALE
    elif field == "changed_files":
        repository.pull["changed_files"] += 1
    elif field == "base_tip":
        repository.data[REF]["object"]["sha"] = STALE
    with pytest.raises(ValueError, match=f"^{mod.CHANGED}$"):
        mod.confirm(repository.pull, proof)
    assert repository.calls == ([REF] if field == "base_tip" else [])


@pytest.mark.parametrize(
    "fault",
    [
        "hash",
        "schema",
        "unknown_field",
        "manifest",
        "duplicate_path",
        "bad_content_hash",
        "unsafe_mode",
        "wrong_addition",
        "missing_base_tip",
    ],
)
def test_confirm_rejects_malformed_proof_even_with_recomputed_digest(repository, fault):
    proof = mod.evidence(repository.pull)
    if fault == "hash":
        proof["head_sha"] = STALE
    elif fault == "schema":
        proof["schema_version"] = True
    elif fault == "unknown_field":
        proof["extra"] = "unverified"
    elif fault == "manifest":
        proof["files"] = [{}]
    elif fault == "duplicate_path":
        proof["files"][1]["path"] = proof["files"][0]["path"]
    elif fault == "bad_content_hash":
        proof["files"][0]["head_content_sha256"] = "a" * 40
    elif fault == "unsafe_mode":
        proof["files"][0]["head_mode"] = "120000"
    elif fault == "wrong_addition":
        proof["files"][0]["status"] = "added"
    elif fault == "missing_base_tip":
        del proof["base_tip_sha"]
    if fault != "hash":
        proof["manifest_sha256"] = proof_hash(proof)
    with pytest.raises(ValueError, match=f"^{mod.REFUSAL}$"):
        mod.confirm(repository.pull, proof)


def test_transport_or_cooldown_failure_is_not_a_partial_proof(repository, monkeypatch):
    def unavailable(*_args):
        raise RuntimeError("GitHub read deferred")

    proof = mod.evidence(repository.pull)
    monkeypatch.setattr(mod, "_get", unavailable)
    with pytest.raises(RuntimeError, match="GitHub read deferred"):
        mod.evidence(repository.pull)
    with pytest.raises(RuntimeError, match="GitHub read deferred"):
        mod.confirm(repository.pull, proof)
