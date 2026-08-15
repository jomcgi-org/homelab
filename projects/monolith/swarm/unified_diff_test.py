from swarm.unified_diff import parse_unified_diff


def test_added_file():
    files = parse_unified_diff(
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+one\n"
        "+two\n"
    )
    assert files == [
        {
            "path": "new.py",
            "status": "added",
            "additions": 2,
            "deletions": 0,
            "changes": 2,
            "patch": "@@ -0,0 +1,2 @@\n+one\n+two\n",
        }
    ]


def test_removed_file():
    files = parse_unified_diff(
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-gone\n"
    )
    assert files[0] == {
        "path": "old.py",
        "status": "removed",
        "additions": 0,
        "deletions": 1,
        "changes": 1,
        "patch": "@@ -1 +0,0 @@\n-gone\n",
    }


def test_renamed_file():
    files = parse_unified_diff(
        "diff --git a/old name.py b/new name.py\n"
        "similarity index 80%\n"
        "rename from old name.py\n"
        "rename to new name.py\n"
        "--- a/old name.py\n"
        "+++ b/new name.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert files[0]["path"] == "new name.py"
    assert files[0]["status"] == "renamed"
    assert files[0]["changes"] == 2


def test_binary_file():
    files = parse_unified_diff(
        "diff --git a/image.png b/image.png\n"
        "index 123..456 100644\n"
        "Binary files a/image.png and b/image.png differ\n"
    )
    assert files[0] == {
        "path": "image.png",
        "status": "modified",
        "additions": 0,
        "deletions": 0,
        "changes": 0,
        "patch": None,
    }


def test_empty_diff():
    assert parse_unified_diff("") == []


def test_no_trailing_newline():
    files = parse_unified_diff(
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
        "\\ No newline at end of file"
    )
    assert files[0]["patch"].endswith("\\ No newline at end of file")
    assert files[0]["additions"] == 1
    assert files[0]["deletions"] == 1


def test_content_that_looks_like_file_headers_is_counted():
    files = parse_unified_diff(
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "---- removed content\n"
        "++++ added content\n"
    )
    assert files[0]["additions"] == 1
    assert files[0]["deletions"] == 1


def test_removed_sql_comment_does_not_become_the_path():
    # A removed line reading "-- comment" appears as "--- comment", which is a
    # valid old-file header shape. Every migration in this repo opens that way,
    # so deleting one must not rename the file to its own first comment.
    files = parse_unified_diff(
        "diff --git a/chart/migrations/x.sql b/chart/migrations/x.sql\n"
        "deleted file mode 100644\n"
        "--- a/chart/migrations/x.sql\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "--- Store the server-owned intent separately.\n"
        "-ALTER TABLE agent_sessions.agent_turns ADD COLUMN prompt_intent TEXT;\n"
    )
    assert files[0]["path"] == "chart/migrations/x.sql"
    assert files[0]["status"] == "removed"
    assert files[0]["deletions"] == 2


def test_added_line_starting_with_plus_plus_does_not_retarget_the_file():
    # An added line reading "++ x" appears as "+++ x", a valid new-file header.
    # Reading it as one would attach this hunk to a different file entirely,
    # which is the worst failure available here: a real diff under a wrong path.
    files = parse_unified_diff(
        "diff --git a/docs/example.md b/docs/example.md\n"
        "--- a/docs/example.md\n"
        "+++ b/docs/example.md\n"
        "@@ -1 +1,2 @@\n"
        " intro\n"
        "+++ b/some/other/file.py\n"
    )
    assert files[0]["path"] == "docs/example.md"
    assert files[0]["additions"] == 1
