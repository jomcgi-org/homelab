"""Offline adversarial tests for the privileged publisher's trust boundaries."""

import copy
import io
import json
import stat
import unittest
import warnings
import zipfile
from unittest import mock

import publish

A = "a" * 40
B = "b" * 40
C = "c" * 40
URL = "https://jomcgi-org.github.io/homelab/"
HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    f'<meta http-equiv="Content-Security-Policy" content="{publish.CSP}">'
    "</head><body><h1>Synthetic fixture</h1></body></html>"
).encode()


def archive(entries, compression=zipfile.ZIP_DEFLATED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as handle:
        for name, data in entries:
            handle.writestr(name, data)
    return output.getvalue()


def pr(number=42, sha=A, state="open"):
    return {
        "number": number,
        "state": state,
        "base": {"ref": "main", "repo": {"full_name": publish.REPOSITORY}},
        "head": {"sha": sha, "repo": {"full_name": publish.REPOSITORY}},
    }


def run_and_workflow():
    run = {
        "id": 9,
        "run_attempt": 1,
        "repository": {"full_name": publish.REPOSITORY},
        "head_repository": {"full_name": publish.REPOSITORY},
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "workflow_id": 123,
        "path": publish.BUILD_WORKFLOW,
        "head_sha": A,
        "pull_requests": [{"number": 42, "head": {"sha": A}}],
    }
    workflow = {"id": 123, "path": publish.BUILD_WORKFLOW, "name": publish.BUILD_NAME}
    return run, workflow


class ArchiveTests(unittest.TestCase):
    def test_flat_zip_preserves_exact_tested_bytes(self):
        data = {
            "index.html": HTML,
            "assets/main.js": b"console.log('fixture')",
            "assets/style.css": b"body{}",
            "assets/pic.png": b"fake png",
            "fixture.json": b"{}",
            "assets/SchibstedGrotesk-LICENSE.txt": b"SIL OPEN FONT LICENSE Version 1.1",
        }
        self.assertEqual(publish.extract_assets(archive(data.items())), data)

    def test_reject_traversal_absolute_windows_hidden_and_unknown_extensions(self):
        names = [
            "../secret.html",
            "/index.html",
            "a/../../index.html",
            "a\\index.html",
            "C:/index.html",
            "./index.html",
            "a//index.html",
            ".git/config",
            ".github/workflows/run.yml",
            "a/.hidden.js",
            "CNAME",
            "deploy.py",
            "payload.tar",
            "payload.zip",
            "index.html%00.js",
            "active.svg",
        ]
        for name in names:
            with self.subTest(name=name), self.assertRaises(publish.Unsafe):
                publish.extract_assets(archive([("index.html", HTML), (name, b"bad")]))

    def test_reject_unix_symlink_and_special_files(self):
        for mode in [
            stat.S_IFLNK,
            stat.S_IFIFO,
            stat.S_IFSOCK,
            stat.S_IFBLK,
            stat.S_IFCHR,
        ]:
            entry = zipfile.ZipInfo("escape.js")
            entry.create_system = 3
            entry.external_attr = (mode | 0o777) << 16
            with self.subTest(mode=mode), self.assertRaises(publish.Unsafe):
                publish.extract_assets(
                    archive([("index.html", HTML), (entry, "../../outside")])
                )

    def test_reject_duplicate_and_case_collision(self):
        for name in ["index.html", "INDEX.HTML"]:
            with self.subTest(name=name), warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                raw = archive([("index.html", HTML), (name, HTML)])
            with self.assertRaises(publish.Unsafe):
                publish.extract_assets(raw)

    def test_reject_file_directory_collision(self):
        with self.assertRaises(publish.Unsafe):
            publish.extract_assets(
                archive([("index.html", HTML), ("a.js", b"x"), ("a.js/b.js", b"x")])
            )

    def test_directory_entries_are_validated(self):
        self.assertIn(
            "index.html",
            publish.extract_assets(archive([("assets/", b""), ("index.html", HTML)])),
        )
        with self.assertRaises(publish.Unsafe):
            publish.extract_assets(archive([("../", b""), ("index.html", HTML)]))

    def test_limits_compressed_expanded_member_and_count(self):
        raw = archive([("index.html", HTML), ("a.js", b"a" * 5000)])
        with (
            mock.patch.object(publish, "MAX_ARCHIVE", len(raw) - 1),
            self.assertRaises(publish.Unsafe),
        ):
            publish.extract_assets(raw)
        with (
            mock.patch.object(publish, "MAX_ARCHIVE", 5000),
            self.assertRaises(publish.Unsafe),
        ):
            publish.extract_assets(raw)
        with (
            mock.patch.object(publish, "MAX_FILE", 4999),
            self.assertRaises(publish.Unsafe),
        ):
            publish.extract_assets(raw)
        with (
            mock.patch.object(publish, "MAX_FILES", 1),
            self.assertRaises(publish.Unsafe),
        ):
            publish.extract_assets(raw)

    def test_reject_missing_root_index_and_nested_tar(self):
        for data in [[("nested/index.html", HTML)], [("artifact.tar", b"not a tar")]]:
            with self.assertRaises(publish.Unsafe):
                publish.extract_assets(archive(data))

    def test_reject_unsupported_compression(self):
        with self.assertRaises(publish.Unsafe):
            publish.extract_assets(archive([("index.html", HTML)], zipfile.ZIP_BZIP2))

    def test_csp_must_precede_active_content_in_every_html(self):
        bad = [
            HTML.replace(
                b"<html>", b'<html style="background:url(https://example.com)">'
            ),
            HTML.replace(b"<head>", b"<head><!--><script>alert(1)</script>-->"),
            HTML.replace(b"<head>", b"<head><title>"),
            HTML.replace(
                b"<head>", b'<head><script src="https://example.com/a.js"></script>'
            ),
            HTML.replace(b"connect-src 'none'", b"connect-src *"),
            HTML.replace(b"<head>", b"<body>"),
            HTML.replace(b"utf-8", b"utf-16"),
            b"<!-- " + HTML + b" -->",
            HTML.replace(b"<head>", b"<head>outside text"),
            HTML.replace(
                b"<head>",
                b'<head><meta http-equiv="refresh" content="0;url=https://example.com">',
            ),
        ]
        for data in bad:
            with self.subTest(data=data[:80]), self.assertRaises(publish.Unsafe):
                publish.extract_assets(
                    archive([("index.html", HTML), ("other.html", data)])
                )

    def test_reject_html5_head_recovery_before_csp(self):
        for tag in [b"</body>", b"</html>", b"</p>", b"<head/>", b"<html/>"]:
            with self.subTest(tag=tag), self.assertRaises(publish.Unsafe):
                publish.validate_html(HTML.replace(b"<head>", tag + b"<head>"))
        publish.validate_html(
            HTML.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8" />')
        )

    def test_only_html_whitespace_may_precede_csp(self):
        for content in ["\u00a0", "\u2000", "\v", "&nbsp;"]:
            with self.subTest(content=content), self.assertRaises(publish.Unsafe):
                publish.validate_html(
                    HTML.replace(b"<head>", b"<head>" + content.encode())
                )
        publish.validate_html(HTML.replace(b"<head>", b"<head> \t\n\f\r"))


class ProvenanceTests(unittest.TestCase):
    def test_exact_successful_pr_head_and_rerun(self):
        run, workflow = run_and_workflow()
        self.assertEqual(
            publish.validate_run(run, workflow, copy.deepcopy(run), pr()), (42, A)
        )
        run["run_attempt"] = 2
        self.assertEqual(
            publish.validate_run(run, workflow, copy.deepcopy(run), pr()), (42, A)
        )

    def test_run_path_accepts_github_ref_suffix(self):
        run, workflow = run_and_workflow()
        for suffix in ["@main", "@refs/pull/42/merge"]:
            run["path"] = publish.BUILD_WORKFLOW + suffix
            self.assertEqual(
                publish.validate_run(run, workflow, copy.deepcopy(run), pr()), (42, A)
            )
        run["path"] = ".github/workflows/evil.yml@main"
        with self.assertRaises(publish.Unsafe):
            publish.validate_run(run, workflow, copy.deepcopy(run), pr())

    def test_failed_push_wrong_repository_workflow_path_and_attempt(self):
        run, workflow = run_and_workflow()
        for field, value in [
            ("conclusion", "failure"),
            ("status", "in_progress"),
            ("event", "push"),
            ("path", ".github/workflows/evil.yml"),
            ("workflow_id", 999),
            ("repository", {"full_name": "attacker/repo"}),
            ("pull_requests", []),
            ("run_attempt", 2),
            ("head_sha", B),
        ]:
            changed = {**run, field: value}
            with self.subTest(field=field), self.assertRaises(publish.Unsafe):
                publish.validate_run(changed, workflow, run, pr())

    def test_closed_and_newer_head_are_stale(self):
        run, workflow = run_and_workflow()
        for pull in [pr(state="closed"), pr(sha=B)]:
            with self.assertRaises(publish.StalePreview):
                publish.validate_run(run, workflow, run, pull)

    def test_pages_url_from_api_must_be_dedicated_github_io(self):
        self.assertEqual(
            publish.pages_url(
                {"build_type": "workflow", "html_url": URL, "cname": None}
            ),
            URL,
        )
        for update in [
            {"cname": "jomcgi.dev"},
            {"build_type": "legacy"},
            {"html_url": "https://jomcgi.dev/"},
            {"html_url": "https://jomcgi-org.github.io/other/"},
            {"html_url": "https://jomcgi-org.github.io.evil.test/homelab/"},
            {"html_url": URL + "?x=y"},
        ]:
            with self.subTest(update=update), self.assertRaises(publish.Unsafe):
                publish.pages_url({"build_type": "workflow", "html_url": URL, **update})


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.now = 2_000_000_000
        self.state = publish.empty_state(URL)
        self.files = {}
        publish.add_preview(
            self.state,
            self.files,
            42,
            A,
            {"index.html": HTML, "asset.js": b"42"},
            self.now,
            9,
        )
        publish.add_preview(
            self.state,
            self.files,
            43,
            B,
            {"index.html": HTML, "asset.js": b"43"},
            self.now,
            10,
        )
        publish.finish_site(self.state, self.files)

    def test_add_second_pr_preserves_first(self):
        self.assertEqual(self.files[f"site/pr/42/{A}/asset.js"], b"42")
        self.assertEqual(self.files[f"site/pr/43/{B}/asset.js"], b"43")
        self.assertIn(A.encode(), self.files["site/pr/42/index.html"])

    def test_new_sha_replaces_only_that_pr_and_pointer(self):
        publish.add_preview(
            self.state, self.files, 42, C, {"index.html": HTML}, self.now + 1, 11
        )
        self.assertNotIn(f"site/pr/42/{A}/index.html", self.files)
        self.assertIn(f"site/pr/42/{C}/index.html", self.files)
        self.assertIn(f"site/pr/43/{B}/asset.js", self.files)
        self.assertIn(C.encode(), self.files["site/pr/42/index.html"])

    def test_closed_and_ttl_cleanup_preserve_other_pr(self):
        for pull, time_delta in [(pr(state="closed"), 0), (pr(), publish.TTL_SECONDS)]:
            state, files = copy.deepcopy(self.state), self.files.copy()
            state["previews"]["43"]["updated_at"] += time_delta
            publish.reconcile(
                state, files, {"42": pull, "43": pr(43, B)}, self.now + time_delta
            )
            self.assertNotIn("42", state["previews"])
            self.assertFalse(any(path.startswith("site/pr/42/") for path in files))
            self.assertIn(f"site/pr/43/{B}/asset.js", files)

    def test_head_advance_preserves_latest_passing_preview(self):
        previous = self.files.copy()
        publish.reconcile(
            self.state, self.files, {"42": pr(sha=C), "43": pr(43, B)}, self.now + 10
        )
        self.assertEqual(self.files, previous)
        self.assertEqual(self.state["previews"]["42"]["sha"], A)

    def test_identical_rerun_refreshes_ttl_but_changed_bytes_fail(self):
        publish.add_preview(
            self.state,
            self.files,
            42,
            A,
            {"index.html": HTML, "asset.js": b"42"},
            self.now + 20,
            9,
        )
        self.assertEqual(self.state["previews"]["42"]["updated_at"], self.now + 20)
        with self.assertRaises(publish.Unsafe):
            publish.add_preview(
                self.state,
                self.files,
                42,
                A,
                {"index.html": HTML, "asset.js": b"changed"},
                self.now + 30,
                9,
            )

    def test_state_marker_repository_url_and_paths_fail_closed(self):
        for key, value in [
            ("schema", "unrelated-site"),
            ("repository", "other/repo"),
            ("pages_url", "https://example.com"),
        ]:
            with self.assertRaises(publish.Unsafe):
                publish.validate_state({**self.state, key: value}, URL)
        for path in [
            "CNAME",
            "site/CNAME",
            "site/.git/config",
            "site/pr/42/../../index.html",
            "publisher.py",
        ]:
            with self.assertRaises(publish.Unsafe):
                publish.state_path(path)

    def test_aggregate_limit_fails_before_publication(self):
        with (
            mock.patch.object(publish, "MAX_SITE", 2),
            self.assertRaises(publish.Unsafe),
        ):
            publish.finish_site(self.state, self.files)

    def test_absent_state_requires_explicit_owned_site_bootstrap(self):
        api = mock.Mock()
        api.optional.return_value = None
        with self.assertRaises(publish.Unsafe):
            publish.load_state(api, URL, False)
        parent, state, files = publish.load_state(api, URL, True)
        self.assertIsNone(parent)
        self.assertEqual(state["schema"], publish.MARKER)
        self.assertEqual(files, {})


class GuardAndCommentTests(unittest.TestCase):
    def test_guard_checks_state_commit_and_every_current_head(self):
        state = publish.empty_state(URL)
        state["previews"] = {"42": {"sha": A, "updated_at": 2_000_000_000}}
        import base64

        responses = {
            "/pages": {"build_type": "workflow", "html_url": URL},
            f"/git/ref/heads/{publish.STATE_BRANCH}": {"object": {"sha": C}},
            f"/contents/state.json?ref={C}": {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(json.dumps(state).encode()).decode(),
            },
            "/pulls/42": pr(),
        }
        api = mock.Mock()
        api.repo.side_effect = lambda path: responses[path]
        with mock.patch.object(publish.time, "time", return_value=2_000_000_010):
            self.assertEqual(publish.guard(api, C), URL)
            responses["/pulls/42"] = pr(sha=B)
            self.assertEqual(publish.guard(api, C), URL)
            with self.assertRaises(publish.StalePreview):
                publish.guard(api, C, "42", A)
            responses["/pulls/42"] = pr(state="closed")
            with self.assertRaises(publish.Unsafe):
                publish.guard(api, C)
            with self.assertRaises(publish.Unsafe):
                publish.guard(api, A)

    def test_comment_only_updates_matching_bot_marker(self):
        api = mock.Mock()
        api.repo.side_effect = lambda path, **kwargs: (
            {"build_type": "workflow", "html_url": URL}
            if path == "/pages"
            else pr()
            if path == "/pulls/42"
            else None
        )
        api.all.return_value = [
            {
                "id": 3,
                "body": publish.COMMENT_MARKER,
                "user": {"login": "someone", "type": "User"},
            },
            {
                "id": 4,
                "body": publish.COMMENT_MARKER,
                "user": {"login": publish.BOT, "type": "Bot"},
            },
        ]
        publish.comment(api, 42, A, URL)
        self.assertEqual(api.repo.call_args.args[0], "/issues/comments/4")
        self.assertEqual(api.repo.call_args.kwargs["method"], "PATCH")
        self.assertIn(URL + "pr/42/", api.repo.call_args.kwargs["payload"]["body"])

    def test_comment_rejects_unverified_url_and_closed_pr(self):
        api = mock.Mock()
        api.repo.side_effect = lambda path: (
            {"build_type": "workflow", "html_url": URL}
            if path == "/pages"
            else pr(state="closed")
        )
        for url in ["https://example.com/", URL]:
            with self.assertRaises(publish.Unsafe):
                publish.comment(api, 42, A, url)
        api.all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
