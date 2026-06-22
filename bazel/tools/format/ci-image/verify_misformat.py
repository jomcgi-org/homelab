# TEMPORARY verification artifact for the ci/format image (PR #2799).
# Deliberately misformatted so the new bazel-free Format check (running ruff
# from the ci/format image) reformats it via ci-format-bot. Removed before merge.
x = {'a' :1,   'b':2}
def f( a,b ):
    return  a+b
