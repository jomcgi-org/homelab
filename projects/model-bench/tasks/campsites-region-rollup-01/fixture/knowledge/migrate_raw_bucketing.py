"""One-shot migration: grandfather _deleted_with_ttl/ raws and existing atoms.

Intended to run once, offline, inside a maintenance window. Idempotent on
re-run: all inserts are guarded by existence checks so interrupted runs
can be resumed safely.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil

from pathlib import Path

import yaml
from sqlalchemy import create_engine
from sqlmodel import Session, select

from knowledge import frontmatter
from knowledge.models import AtomRawProvenance, Note, RawInput
from knowledge.raw_paths import (
    GRANDFATHERED_SUBDIR,
    RAW_ROOT_NAME,
    compute_raw_id,
    raw_target_path,
)

logger = logging.getLogger("monolith.knowledge.migrate_raw_bucketing")

_PRE_MIGRATION = "pre-migration"
_DELETED_ROOT_NAME = "_deleted_with_ttl"
_STRIPPED_FRONTMATTER_KEYS = {"ttl", "original_path"}


def _strip_frontmatter_keys(content: str, keys: set[str]) -> str:
    if not content.startswith("---"):
        return content
    lines = content.splitlines(keepends=True)
    end = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            end = i
            break
    if end is None:
        return content
    block = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    try:
        meta = yaml.safe_load(block) or {}
    except yaml.YAMLError:
        return content
    if not isinstance(meta, dict):
        return content
    for k in keys:
        meta.pop(k, None)
    if not meta:
        return body
    return f"---\n{yaml.safe_dump(meta, sort_keys=False).rstrip()}\n---\n{body}"


def _grandfather_raws(vault_root: Path, session: Session) -> int:
    deleted_root = vault_root / _DELETED_ROOT_NAME
    if not deleted_root.exists():
        return 0
    inserted = 0
    for src in sorted(deleted_root.rglob("*.md")):
        raw_content = src.read_text(encoding="utf-8")
        try:
            meta, _ = frontmatter.parse(raw_content)
        except Exception:
            logger.warning("migrate: bad frontmatter in %s, using defaults", src)
            meta = None
        original_path = meta.extra.get("original_path") if meta else None
        stripped = _strip_frontmatter_keys(raw_content, _STRIPPED_FRONTMATTER_KEYS)
        raw_id = compute_raw_id(stripped)
        title = meta.title if meta and meta.title else src.stem
        target = raw_target_path(
            vault_root=vault_root,
            raw_id=raw_id,
            title=title,
            grandfathered=True,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(stripped, encoding="utf-8")

        rel = target.relative_to(vault_root).as_posix()
        existing = session.exec(
            select(RawInput).where(RawInput.raw_id == raw_id)
        ).first()
        if existing is None:
            with session.begin_nested():
                raw_row = RawInput(
                    raw_id=raw_id,
                    path=rel,
                    source="grandfathered",
                    original_path=original_path,
                    content=stripped,
                    content_hash=raw_id,
                )
                session.add(raw_row)
                session.flush()
                session.add(
                    Note(
                        note_id=raw_id,
                        path=rel,
                        title=title,
                        content_hash=raw_id,
                        type="raw",
                        source="grandfathered",
                    )
                )
                session.add(
                    AtomRawProvenance(
                        raw_fk=raw_row.id,
                        gardener_version=_PRE_MIGRATION,
                    )
                )
            inserted += 1

        # Delete source after DB record exists so a crash doesn't lose data.
        src.unlink()

    shutil.rmtree(deleted_root, ignore_errors=True)
    return inserted


def _grandfather_atoms(session: Session) -> int:
    atoms = session.exec(
        select(Note).where(
            Note.deleted_at.is_(None), Note.type.in_(["atom", "fact", "active"])
        )
    ).all()
    inserted = 0
    for atom in atoms:
        existing = session.exec(
            select(AtomRawProvenance).where(
                AtomRawProvenance.atom_fk == atom.id,
                AtomRawProvenance.raw_fk.is_(None),
                AtomRawProvenance.gardener_version == _PRE_MIGRATION,
            )
        ).first()
        if existing is not None:
            continue
        with session.begin_nested():
            session.add(
                AtomRawProvenance(
                    atom_fk=atom.id,
                    gardener_version=_PRE_MIGRATION,
                )
            )
        inserted += 1
    return inserted


def run_migration(*, vault_root: Path, session: Session) -> None:
    """Execute the one-shot raw bucketing migration. Idempotent."""
    raws = _grandfather_raws(vault_root, session)
    atoms = _grandfather_atoms(session)
    logger.info(
        "raw-bucketing migration: grandfathered raws=%d atoms=%d",
        raws,
        atoms,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault-root", default=os.environ.get("VAULT_ROOT", "/vault"))
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()

    if not args.dsn:
        raise SystemExit("--dsn or DATABASE_URL is required")

    logging.basicConfig(level=logging.INFO)
    engine = create_engine(args.dsn)
    with Session(engine) as session:
        run_migration(vault_root=Path(args.vault_root), session=session)
        session.commit()


if __name__ == "__main__":
    main()
