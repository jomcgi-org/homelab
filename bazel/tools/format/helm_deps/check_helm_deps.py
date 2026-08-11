"""Guard the two Helm invariants that fail silently and deploy the wrong thing.

Both were live bugs on 2026-08-11, and neither is caught by a linter, a bazel
test, or `helm lint`.

STALE TARBALL. A consuming chart vendors its `file://` dependencies as committed
`charts/*.tgz`, and `helm_chart` globs `**/*`, so the committed tarball is what
Bazel packages and ships. `sync-helm-deps.sh` rebuilds it only when the library
*version* changes, never on content. Editing a library template without bumping
the library version therefore leaves the fix in the source while the stale
tarball deploys: clean diff, green CI, nothing rolls.

BARE TAG. `helm_images_values` injects `repository`, `tag` and `digest` for
every image in a `helm_chart(images = {...})` map. The tag is build-timestamped
so it moves on every commit to main, while `push-changed.sh` skips pushing an
image whose content digest is already published. A chart rendering
`repository:tag` therefore pins a tag that was frequently never created, which
is an ImagePullBackOff. That wedged monolith-public for ~11h (PR #4680) and was
latent in two more charts (PR #4681).

Runs against the full working tree from the CI format step, because a sandboxed
bazel test cannot list projects/* on RBE. The pure logic is pinned by
check_helm_deps_test.py. Deliberately no `helm` dependency: the CI format runner
does not have the helm CLI, which is why sync-helm-deps.sh is local-only and
why this check has to compare tarballs by hand.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tarfile
from pathlib import Path

# Files inside a packaged chart whose drift changes what deploys.
#
# Chart.yaml is compared separately, on `version:` alone, because `helm package`
# RE-SERIALISES it: keys come back alphabetised, strings unquoted, and list
# indentation changed. Byte-comparing it reports every chart as stale forever.
# Chart.lock is excluded entirely: it records a resolution timestamp, so it
# differs on every rebuild without meaning anything.
_MEANINGFUL = re.compile(r"^[^/]+/(templates/.*|values\.yaml)$")
_VERSION = re.compile(r"^version:\s*(\S+)", re.MULTILINE)

# `image: "{{ .Values.<path>.repository }}:{{ ... }}"` with no `@` anywhere in
# the rendered string. The `@` test is what lets `repo:tag@digest` through: the
# digest still decides which bytes run (embervm tokenBroker renders that way).
_IMAGE_LINE = re.compile(r'image:\s*"([^"]*\{\{[^"]*)"')
_REPOSITORY_REF = re.compile(r"\.Values\.([A-Za-z0-9_.]+)\.repository")


def declared_version(text: str) -> str | None:
    """The top-level `version:` of a Chart.yaml, whatever the key order."""
    match = _VERSION.search(text)
    return match.group(1).strip("\"'") if match else None


def tarball_entries(raw: bytes) -> tuple[dict[str, bytes], str | None]:
    """The deploy-meaningful members of a .tgz, plus its declared version."""
    entries: dict[str, bytes] = {}
    version = None
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            if member.name.count("/") == 1 and member.name.endswith("/Chart.yaml"):
                version = declared_version(handle.read().decode())
            elif _MEANINGFUL.match(member.name):
                entries[member.name] = handle.read()
    return entries, version


def chart_name(chart_dir: Path) -> str:
    """The chart's declared name, which is what `helm package` keys members by.

    NOT the directory name. `homelab-library` lives in `.../homelab-library/chart`
    and `cf-ingress` in `.../cf-ingress-library`, so keying on the directory makes
    every member look both added and removed, and every chart look stale.
    """
    match = re.search(
        r"^name:\s*(\S+)", (chart_dir / "Chart.yaml").read_text(), re.MULTILINE
    )
    return match.group(1) if match else chart_dir.name


def source_entries(chart_dir: Path) -> dict[str, bytes]:
    """The same view of an unpacked source chart, keyed the same way."""
    entries: dict[str, bytes] = {}
    name = chart_name(chart_dir)
    for path in sorted(chart_dir.rglob("*")):
        if not path.is_file():
            continue
        member = f"{name}/{path.relative_to(chart_dir).as_posix()}"
        if _MEANINGFUL.match(member):
            entries[member] = path.read_bytes()
    return entries


def diff_entries(packaged: dict[str, bytes], source: dict[str, bytes]) -> list[str]:
    """Names that differ between a packaged chart and its source, sorted."""
    return sorted(
        set(packaged)
        .symmetric_difference(source)
        .union(k for k in set(packaged) & set(source) if packaged[k] != source[k])
    )


def bare_tag_refs(template_text: str, managed_paths: set[str]) -> list[str]:
    """Image renders that pin a bazel-managed image by tag with no digest.

    Only paths in the chart's `helm_chart(images = ...)` map are managed, so an
    upstream image pinned by tag (cloudflare-gateway's envoy, embervm's
    servingEnvoy) is correctly ignored: nothing injects a digest for it and
    nothing skips pushing it.
    """
    offenders = []
    for line in template_text.splitlines():
        match = _IMAGE_LINE.search(line)
        if not match:
            continue
        rendered = match.group(1)
        if "@" in rendered:
            continue
        for path in _REPOSITORY_REF.findall(rendered):
            if path in managed_paths:
                offenders.append(f"{path}: {line.strip()}")
    return offenders


def parse_managed_paths(build_text: str) -> set[str]:
    """The yaml paths in every `helm_chart(images = {...})` in one BUILD file.

    A path here is a promise that helm_images_values will inject a digest at
    that key, which is exactly the set that must not deploy by tag.
    """
    paths: set[str] = set()
    for block in re.findall(r"images\s*=\s*\{(.*?)\}", build_text, re.DOTALL):
        paths.update(re.findall(r'"([^"]+)"\s*:', block))
    return paths


def _dependency_dirs(chart_yaml: Path) -> list[tuple[str, Path]]:
    """(name, resolved source dir) for each `file://` dependency of a chart."""
    deps = []
    text = chart_yaml.read_text()
    for name, repo in re.findall(
        r"-\s*name:\s*(\S+)\s*\n\s*version:.*\n\s*repository:\s*\"?(file://[^\"\s]+)",
        text,
    ):
        deps.append((name, (chart_yaml.parent / repo[len("file://") :]).resolve()))
    return deps


def check_repo(root: Path) -> list[str]:
    """Every violation in the tree, as human-readable lines."""
    problems: list[str] = []

    for chart_yaml in sorted(root.glob("projects/**/Chart.yaml")):
        for name, source_dir in _dependency_dirs(chart_yaml):
            if not source_dir.is_dir():
                problems.append(
                    f"{chart_yaml}: file:// dependency {name} -> {source_dir} is missing"
                )
                continue
            tarballs = sorted((chart_yaml.parent / "charts").glob(f"{name}-*.tgz"))
            if not tarballs:
                problems.append(
                    f"{chart_yaml.parent}/charts: no {name}-*.tgz committed; "
                    f"bazel globs charts/**, so the dependency would not ship. "
                    f"Run ./bazel/tools/format/sync-helm-deps.sh"
                )
                continue
            if len(tarballs) > 1:
                problems.append(
                    f"{chart_yaml.parent}/charts: {len(tarballs)} {name}-*.tgz committed "
                    f"({', '.join(t.name for t in tarballs)}); a partial sync left a stale one behind"
                )
                continue
            packaged, packaged_version = tarball_entries(tarballs[0].read_bytes())
            drift = diff_entries(packaged, source_entries(source_dir))
            if drift:
                problems.append(
                    f"{tarballs[0]} is STALE against {source_dir}: {', '.join(drift)}. "
                    f"The committed tarball is what ships, so this change would not deploy. "
                    f"Run ./bazel/tools/format/sync-helm-deps.sh"
                )
            source_version = declared_version((source_dir / "Chart.yaml").read_text())
            if packaged_version != source_version:
                problems.append(
                    f"{tarballs[0]} declares version {packaged_version} but "
                    f"{source_dir}/Chart.yaml says {source_version}. "
                    f"Run ./bazel/tools/format/sync-helm-deps.sh"
                )

    for build in sorted(root.glob("projects/**/BUILD")):
        managed = parse_managed_paths(build.read_text())
        if not managed:
            continue
        for template in sorted(build.parent.rglob("templates/*")):
            if (
                template.suffix not in (".yaml", ".yml", ".tpl")
                or not template.is_file()
            ):
                continue
            for offender in bare_tag_refs(template.read_text(), managed):
                problems.append(
                    f"{template}: deploys a bazel-managed image by tag, not digest. "
                    f"The build-timestamped tag is often never pushed "
                    f"(push-changed.sh skips content-identical images), so this is an "
                    f"ImagePullBackOff waiting to happen. {offender}"
                )

    return problems


def stale_chart_dirs(root: Path) -> list[Path]:
    """Consumer chart directories whose vendored tarballs need rebuilding.

    sync-helm-deps.sh drives `helm dependency update` off this rather than
    reimplementing the comparison in bash, so there is one definition of "stale"
    and it is the one pinned by check_helm_deps_test.py.
    """
    stale: list[Path] = []
    for chart_yaml in sorted(root.glob("projects/**/Chart.yaml")):
        for name, source_dir in _dependency_dirs(chart_yaml):
            if not source_dir.is_dir():
                continue
            tarballs = sorted((chart_yaml.parent / "charts").glob(f"{name}-*.tgz"))
            if len(tarballs) != 1:
                stale.append(chart_yaml.parent)
                break
            packaged, packaged_version = tarball_entries(tarballs[0].read_bytes())
            if diff_entries(
                packaged, source_entries(source_dir)
            ) or packaged_version != declared_version(
                (source_dir / "Chart.yaml").read_text()
            ):
                stale.append(chart_yaml.parent)
                break
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root to check")
    parser.add_argument(
        "--list-stale",
        action="store_true",
        help="print chart dirs needing `helm dependency update`, one per line, and exit 0",
    )
    args = parser.parse_args()

    if args.list_stale:
        for chart_dir in stale_chart_dirs(Path(args.root).resolve()):
            print(chart_dir)
        return 0

    problems = check_repo(Path(args.root).resolve())
    if problems:
        print("Helm dependency/image check FAILED:\n", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}\n", file=sys.stderr)
        return 1
    print("Helm dependency/image check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
