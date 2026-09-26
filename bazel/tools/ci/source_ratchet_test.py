"""Tests for the source ratchet (the rules removed with custom Semgrep, #4777)."""

from __future__ import annotations

import textwrap

import source_ratchet as ratchet


def _rules(path: str, text: str) -> list[str]:
    return [f.rule for f in ratchet.scan(path, textwrap.dedent(text))]


def _new(path: str, before: str | None, after: str) -> list[str]:
    after = textwrap.dedent(after)
    before = textwrap.dedent(before) if before is not None else None
    return [f.rule for _, f in ratchet.new_findings([(path, before)], [(path, after)])]


# --- image-digest ---------------------------------------------------------

VALUES = "projects/svc/deploy/values.yaml"


def test_digest_on_our_image_is_flagged():
    assert _rules(
        VALUES,
        """\
        image:
          repository: ghcr.io/jomcgi/homelab/projects/svc/image
          tag: "main@sha256:abc"
        """,
    ) == ["image-digest"]


def test_inline_digest_on_our_image_is_flagged():
    assert _rules(VALUES, 'image: "ghcr.io/jomcgi/homelab/x@sha256:abc"\n') == [
        "image-digest"
    ]


def test_third_party_digest_pin_is_fine():
    assert (
        _rules(
            VALUES,
            """\
            image:
              repository: envoyproxy/envoy
              tag: "v1.39-latest@sha256:abc"
            """,
        )
        == []
    )


def test_build_time_digest_placeholder_is_fine():
    assert (
        _rules(
            VALUES,
            """\
            image:
              repository: ghcr.io/jomcgi/homelab/x
              digest: sha256:0000
            """,
        )
        == []
    )


def test_digest_with_tag_above_repository_is_flagged():
    assert _rules(
        VALUES,
        """\
        image:
          tag: "main@sha256:abc"
          # a comment
          repository: ghcr.io/jomcgi/homelab/x
        """,
    ) == ["image-digest"]


def test_digest_in_list_item_or_split_registry_is_flagged():
    assert _rules(
        VALUES,
        """\
        images:
          - repository: ghcr.io/jomcgi/homelab/x
            tag: "main@sha256:abc"
          - repository: envoyproxy/envoy
            tag: "v1@sha256:abc"
        split:
          registry: ghcr.io
          repository: jomcgi/homelab/y
          tag: "v1@sha256:def"
        """,
    ) == ["image-digest", "image-digest"]


def test_sibling_repository_at_other_indent_is_ignored():
    assert (
        _rules(
            VALUES,
            """\
            repository: ghcr.io/jomcgi/homelab/x
            sidecar:
              repository: envoyproxy/envoy
              tag: "v1@sha256:abc"
            """,
        )
        == []
    )


# --- svc-url --------------------------------------------------------------


def test_svc_url_in_source_is_flagged():
    assert _rules(
        "projects/a/main.go", 'const u = "http://x.ns.svc.cluster.local"\n'
    ) == ["svc-url"]


def test_svc_url_in_comment_test_or_values_is_fine():
    assert _rules("projects/a/main.go", "// never x.ns.svc.cluster.local\n") == []
    assert _rules("projects/a/main_test.go", 'u := "x.svc.cluster.local"\n') == []
    assert _rules("projects/a/tests/t.py", 'U = "x.svc.cluster.local"\n') == []
    assert _rules(VALUES, "url: http://x.ns.svc.cluster.local\n") == []


def test_allow_marker_opts_out_for_that_rule_only():
    line = 'U = "x.svc.cluster.local"  # ratchet-allow: svc-url (resolver fixture)\n'
    assert _rules("projects/a/a.py", line) == []
    other = 'U = "x.svc.cluster.local"  # ratchet-allow: image-digest\n'
    assert _rules("projects/a/a.py", other) == ["svc-url"]


# --- kubectl-mutate -------------------------------------------------------


def test_kubectl_write_in_script_is_flagged():
    assert _rules("scripts/x.sh", "kubectl apply -f foo.yaml\n") == ["kubectl-mutate"]
    assert _rules("scripts/x.sh", "kubectl -n a rollout restart deploy/b\n") == [
        "kubectl-mutate"
    ]


def test_kubectl_write_hidden_behind_a_quoted_mention_is_flagged():
    for line in [
        "log 'kubectl delete' ; kubectl delete pod x",
        'log "it\'s gone"; kubectl delete pod x',
        "kubectl -nargocd delete pod x",
        "helm upgrade --install x ./chart",
    ]:
        assert _rules("scripts/x.sh", line + "\n") == ["kubectl-mutate"], line


def test_kubectl_in_heredoc_and_resource_names_is_fine():
    script = """\
    cat <<EOF
    Run kubectl delete pod x yourself.
    EOF
    kubectl get deploy scale-test
    kubectl get cm patch-config -n x
    """
    assert _rules("scripts/x.sh", script) == []


def test_kubectl_reads_dry_runs_messages_and_tokens_are_fine():
    for line in [
        "kubectl get pods -n a",
        "kubectl apply --dry-run=client -f x.yaml",
        'echo "run kubectl apply -f x.yaml yourself"',
        "# kubectl delete pod x",
        "kubectl create token sa -n a",
    ]:
        assert _rules("scripts/x.sh", line + "\n") == [], line


# --- Python Session rules -------------------------------------------------

PY = "projects/monolith/x/jobs.py"


def test_sync_session_in_async_def_is_flagged():
    assert _rules(
        PY,
        """\
        async def handler(session):
            rows = session.exec(select(A)).all()
            session.commit()
        """,
    ) == ["sync-session", "sync-session"]


def test_scheduled_coroutines_and_http_sessions_are_fine():
    assert (
        _rules(
            PY,
            """\
            async def fetch(session, urls):
                await asyncio.gather(session.execute(a), session.execute(b))
                asyncio.create_task(session.commit())
                await asyncio.wait_for(session.execute(q), 5)
                async with session.get(url) as resp:
                    pass
                await asyncio.gather(*(session.get(u) for u in urls))
            """,
        )
        == []
    )


def test_awaited_async_session_and_nested_sync_helper_are_fine():
    assert (
        _rules(
            PY,
            """\
            async def handler(session):
                await session.execute(q)

                def _sync(engine):
                    with Session(engine) as session:
                        session.commit()

                await asyncio.to_thread(_sync, engine)
            """,
        )
        == []
    )


def test_session_into_to_thread_is_flagged():
    assert (
        _rules(
            PY,
            """\
        async def handler(session, loop):
            await asyncio.to_thread(_work, session)
            await asyncio.to_thread(_work, session=session)
            await loop.run_in_executor(None, _work, session)
        """,
        )
        == ["session-to-thread"] * 3
    )


def test_session_add_in_loop_is_flagged():
    assert _rules(
        PY,
        """\
        def save(session, rows):
            for r in rows:
                session.add(r)
            session.commit()
        """,
    ) == ["session-add-loop"]


def test_session_add_in_loop_with_per_iteration_commit_is_fine():
    assert (
        _rules(
            PY,
            """\
            def save(session, rows):
                for r in rows:
                    with session.begin_nested():
                        session.add(r)
                while more():
                    session.add(next_row())
                    session.commit()
            """,
        )
        == []
    )


def test_unparseable_python_is_skipped():
    assert _rules(PY, "def (:\n") == []


# --- ratchet semantics ----------------------------------------------------


def test_existing_findings_are_grandfathered():
    before = 'U = "a.svc.cluster.local"\n'
    after = '"""Doc."""\nU = "a.svc.cluster.local"\n'
    assert _new("projects/a/a.py", before, after) == []


def test_a_second_copy_of_a_grandfathered_line_is_new():
    before = 'U = "a.svc.cluster.local"\n'
    after = before + before
    assert _new("projects/a/a.py", before, after) == ["svc-url"]


def test_new_file_counts_every_finding():
    assert _new("scripts/x.sh", None, "kubectl delete ns a\n") == ["kubectl-mutate"]


def test_rename_keeps_grandfathered_findings():
    body = 'U = "a.svc.cluster.local"\n'
    found = ratchet.new_findings(
        [("projects/a/a.py", body)], [("projects/b/a.py", body)]
    )
    assert found == []


ASYNC_COMMIT = """\
async def handler(session):
    rows = session.execute(select(A)).scalars().all()
    session.commit()
"""


def test_moving_a_function_between_changed_files_is_not_new():
    found = ratchet.new_findings(
        [("projects/m/a.py", ASYNC_COMMIT), ("projects/m/b.py", "")],
        [("projects/m/a.py", ""), ("projects/m/b.py", ASYNC_COMMIT)],
    )
    assert found == []


def test_reformatting_a_grandfathered_call_is_not_new():
    reflowed = """\
async def handler(session):
    rows = (
        session.execute(
            select(A),
        )
        .scalars()
        .all()
    )
    session.commit()  # noqa: E501
"""
    assert _new(PY, ASYNC_COMMIT, reflowed) == []


def test_a_new_call_in_the_same_function_is_new():
    grown = ASYNC_COMMIT + "    session.flush()\n"
    assert _new(PY, ASYNC_COMMIT, grown) == ["sync-session"]
