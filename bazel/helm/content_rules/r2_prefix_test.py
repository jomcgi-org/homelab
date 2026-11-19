# invariant: LIB-7
"""Mirrors that share an R2 bucket cannot delete each other's objects (HOME-492).

A factory-library mirror without public.r2Prefix runs rclone sync on the root
of its R2 bucket, and sync deletes what its Garage bucket lacks. That mirror
must list in public.excludePrefixes every prefix that another mirror writes
into the same R2 bucket. Each R2 bucket and prefix pair has one mirror.

The test reads the chart values of every factory application in the BUILD
APPLICATIONS list. coverage_test keeps that list equal to the factory
cluster's kustomization.
"""

from dataclasses import dataclass, field
import os
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Mirror:
    where: str
    r2_bucket: str
    prefix: str | None
    excludes: tuple[str, ...] = field(default=())


def mirrors(charts):
    """The public mirrors in {chart: values}, in a stable order."""
    out = []
    for chart, values in sorted(charts.items()):
        for bucket in (values or {}).get("buckets") or []:
            public = bucket.get("public")
            if public is None:
                continue
            out.append(
                Mirror(
                    where=f"{chart} bucket {bucket['name']}",
                    r2_bucket=public.get("r2Bucket"),
                    prefix=public.get("r2Prefix"),
                    excludes=tuple(public.get("excludePrefixes") or ()),
                )
            )
    return out


def destination(mirror):
    if mirror.prefix is None:
        return f"the root of R2 {mirror.r2_bucket}"
    return f"R2 {mirror.r2_bucket}/{mirror.prefix}/"


def problems(charts):
    found = []
    owners = {}
    found_mirrors = mirrors(charts)
    for mirror in found_mirrors:
        key = (mirror.r2_bucket, mirror.prefix)
        if key in owners:
            found.append(
                f"{mirror.where} and {owners[key].where} both mirror into "
                f"{destination(mirror)}: give each a different public.r2Prefix"
            )
        else:
            owners[key] = mirror
    for mirror in found_mirrors:
        if mirror.prefix is None:
            continue
        for root in found_mirrors:
            if root.r2_bucket != mirror.r2_bucket or root.prefix is not None:
                continue
            if mirror.prefix not in root.excludes:
                found.append(
                    f"{root.where} syncs {destination(root)} and would delete "
                    f"{destination(mirror)} of {mirror.where}: add "
                    f"{mirror.prefix} to its public.excludePrefixes"
                )
    return found


def public(r2_bucket, prefix=None, excludes=None):
    entry = {"r2Bucket": r2_bucket}
    if prefix:
        entry["r2Prefix"] = prefix
    if excludes is not None:
        entry["excludePrefixes"] = excludes
    return entry


def chart(name, **kwargs):
    return {"buckets": [{"name": name, "access": "write", "public": public(**kwargs)}]}


def test_root_mirror_must_exclude_each_prefix():
    charts = {
        "root": chart("root", r2_bucket="factory-public", excludes=["hikes"]),
        "hikes": chart("hikes", r2_bucket="factory-public", prefix="hikes"),
        "stars": chart("stars", r2_bucket="factory-public", prefix="stars"),
    }
    assert problems(charts) == [
        "root bucket root syncs the root of R2 factory-public and would delete "
        "R2 factory-public/stars/ of stars bucket stars: add stars to its "
        "public.excludePrefixes"
    ]
    charts["root"]["buckets"][0]["public"]["excludePrefixes"].append("stars")
    assert problems(charts) == []


def test_each_prefix_has_one_mirror():
    charts = {
        "one": chart("one", r2_bucket="factory-public", prefix="walks"),
        "two": chart("two", r2_bucket="factory-public", prefix="walks"),
    }
    assert problems(charts) == [
        "two bucket two and one bucket one both mirror into "
        "R2 factory-public/walks/: give each a different public.r2Prefix"
    ]


def test_each_r2_bucket_has_one_root_mirror():
    charts = {
        "one": chart("one", r2_bucket="factory-public"),
        "two": chart("two", r2_bucket="factory-public"),
    }
    assert len(problems(charts)) == 1


def test_other_r2_buckets_and_private_buckets_do_not_count():
    charts = {
        "root": chart("root", r2_bucket="factory-public", excludes=[]),
        "other": chart("other", r2_bucket="elsewhere", prefix="hikes"),
        "private": {"buckets": [{"name": "state", "access": "readwrite"}]},
        "none": {"workloads": {}},
        "empty": None,
    }
    assert problems(charts) == []


def test_factory_mirrors_do_not_overlap():
    paths = [Path(p) for p in os.environ["VALUES"].split()]
    # projects/<application>/chart/values.yaml
    charts = {p.parent.parent.name: yaml.safe_load(p.read_text()) for p in paths}
    assert len(charts) == len(paths) == len(os.environ["APPLICATIONS"].split())
    found = mirrors(charts)
    # Guard against reading nothing: factory-public owns the root mirror.
    assert any(m.r2_bucket == "factory-public" and m.prefix is None for m in found), (
        found
    )
    assert problems(charts) == []
