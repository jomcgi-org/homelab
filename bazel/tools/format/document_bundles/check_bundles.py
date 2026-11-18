"""Validate tracked project documents against docs/templates/bundles.yaml."""

import os
import posixpath
import re
import subprocess
from collections import defaultdict
from pathlib import Path, PurePosixPath

import yaml


def markdown(text):
    """Extract prose and completed fenced blocks, hiding examples and comments."""
    prose, blocks = [], []
    fence = None
    for line in re.sub(r"<!--.*?(?:-->|$)", "", text, flags=re.S).splitlines():
        if fence:
            if re.fullmatch(
                r" {0,3}" + fence[0] + "{" + str(len(fence)) + r",}\s*", line
            ):
                blocks.append((start, language))
                fence = None
        elif match := re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line):
            fence, language = match.groups()
            language = language.strip()
            start = len(prose)
        else:
            prose.append(line)
    return prose, blocks


def tables(lines):
    """Yield headers and rows for pipe tables with a delimiter row."""
    cells = [
        line.strip().strip("|").split("|") if "|" in line else [] for line in lines
    ]
    for i, row in enumerate(cells[1:], 1):
        if (
            row
            and len(row) == len(cells[i - 1])
            and all(re.fullmatch(r"\s*:?-+:?\s*", cell) for cell in row)
        ):
            rows = []
            for data in cells[i + 1 :]:
                if len(data) != len(row):
                    break
                rows.append(data)
            yield [cell.strip() for cell in cells[i - 1]], rows


def links(path, lines):
    result = set()
    for target in re.findall(
        r"(?<!!)\[[^\]]*\]\(<?([^\s)>#]+)(?:#[^\s)>]*)?>?\)", "\n".join(lines)
    ):
        result.add(
            PurePosixPath(posixpath.normpath(posixpath.join(path.parent, target)))
        )
    return result


def shape(path, text, rules, files):
    lines, blocks = markdown(text)
    for rule, value in rules.items():
        if rule == "opening_fence":
            first = next(
                (i for i, line in enumerate(lines) if line.startswith("## ")),
                len(lines),
            )
            ok = any(i <= first and lang == value for i, lang in blocks)
        elif rule == "heading":
            ok = any(line.rstrip() == "## " + value for line in lines)
        elif rule == "table_columns":
            ok = any(set(value) <= set(header) for header, _ in tables(lines))
        elif rule == "max_lines":
            ok = len(text.splitlines()) <= value
        elif rule == "forbidden_prefixes":
            ok = not any(line.startswith(tuple(value)) for line in text.splitlines())
        elif rule == "link_if_present":
            target = path.parent / value
            ok = target not in files or target in links(path, lines)
        else:
            raise ValueError(rule)
        if not ok:
            yield f"{rule}: {value}"


def validate(config, files, documents):
    """files includes source paths; only Markdown requires file contents."""
    errors = defaultdict(list)
    dirs = {parent for path in files for parent in path.parents}
    kinds = {}
    for kind, spec in config["kinds"].items():
        if "glob" in spec:
            matches = {
                p
                for p in dirs
                if p.match(spec["glob"])
                and len(p.parts) == len(PurePosixPath(spec["glob"]).parts)
            }
        elif "paths" in spec:
            matches = dirs.intersection(map(PurePosixPath, spec["paths"]))
        elif "extends" in spec:
            matches = {
                p for p in kinds[spec["extends"]] if p / spec["has_directory"] in dirs
            }
        else:
            sources = {
                parent
                for f in files
                if f.suffix in spec["source_suffixes"]
                for parent in f.parents
            }
            matches = {
                p
                for p in sources
                if p.parent in kinds[spec["child_of"]] and p.name not in spec["exclude"]
            }
        kinds[kind] = matches
        for directory in matches:
            for rule in spec["requires"]:
                if "file" in rule:
                    p = directory / rule["file"]
                    if p not in files:
                        errors[p].append("missing required file")
                elif "file_or_link" in rule:
                    p = directory / rule["file_or_link"]
                    readme = directory / rule["link_from"]
                    targets = links(readme, markdown(documents.get(readme, ""))[0])
                    if p not in files and not any(
                        t != p
                        and t in files
                        and t.match(rule["other_target"])
                        and len(t.parts)
                        == len(PurePosixPath(rule["other_target"]).parts)
                        for t in targets
                    ):
                        errors[p].append(
                            f"missing file or link from {readme} to {rule['other_target']}"
                        )
                elif "children_named_in" in rule:
                    p = directory / rule["children_named_in"]
                    prose = "\n".join(markdown(documents.get(p, ""))[0])
                    for child in sorted(d for d in dirs if d.parent == directory):
                        if not mentions(prose, child.name):
                            errors[p].append(f"missing child {child.name}")
                else:
                    p = PurePosixPath(
                        posixpath.normpath(directory / rule["table_row_in"])
                    )
                    if not any(
                        rule["column"] in h
                        and any(
                            mentions(row[h.index(rule["column"])], directory.name)
                            for row in rows
                        )
                        for h, rows in tables(markdown(documents.get(p, ""))[0])
                    ):
                        errors[p].append(f"missing component row for {directory.name}")
            for optional in spec.get("optional", []):
                p = directory / optional["file"]
                parent = PurePosixPath(
                    posixpath.normpath(directory / optional["linked_from"])
                )
                if p in files and p not in links(
                    parent, markdown(documents.get(parent, ""))[0]
                ):
                    errors[p].append(f"missing link from {parent}")
    for p, text in documents.items():
        if str(p) in config["templates"]:
            name = config["templates"][str(p)]
        elif PurePosixPath(config["scope"]) in p.parents:
            name = p.name
            if (
                re.fullmatch(config["name_pattern"], name)
                and name not in config["allowed_names"]
            ):
                errors[p].append("outside allowed_names")
        else:
            continue
        for error in shape(p, text, config["format"].get(name, {}), files):
            errors[p].append(error)
    return errors


def mentions(text, name):
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text)


def report(errors, exemptions):
    for p in sorted(errors.keys() - exemptions):
        yield f"{p}: {', '.join(errors[p])}"
    for p in sorted(exemptions - errors.keys()):
        yield f"{p}: remove stale exemption"


def main():
    if workspace := os.environ.get("BUILD_WORKSPACE_DIRECTORY"):
        os.chdir(workspace)
    config = yaml.safe_load(Path("docs/templates/bundles.yaml").read_text())
    paths = (
        subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard", "-z"])
        .decode()
        .split("\0")
    )
    files = {PurePosixPath(p) for p in paths if p and Path(p).is_file()}
    docs = {
        p: Path(p).read_text()
        for p in files
        if p.suffix == ".md"
        and (p.parts[0] == config["scope"] or str(p) in config["templates"])
    }
    exemptions = Path("bazel/tools/format/document_bundles/exemptions.txt")
    errors = list(
        report(
            validate(config, files, docs),
            set(map(PurePosixPath, exemptions.read_text().splitlines())),
        )
    )
    for error in errors:
        print(error)
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
