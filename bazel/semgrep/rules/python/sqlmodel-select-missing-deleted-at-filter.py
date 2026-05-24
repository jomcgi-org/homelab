# Tests for sqlmodel-select-missing-deleted-at-filter rule.
#
# Background: PR #2350 added a `deleted_at` column to Gap and Note for soft-deletion.
# Any select(Gap) or select(Note) without a corresponding .where(...deleted_at...)
# filter silently returns soft-deleted rows to callers.
from sqlmodel import select


# ruleid: sqlmodel-select-missing-deleted-at-filter
rows = session.exec(select(Gap)).all()


# ruleid: sqlmodel-select-missing-deleted-at-filter
rows = session.exec(select(Note)).all()


# ruleid: sqlmodel-select-missing-deleted-at-filter
rows = session.exec(select(Gap).where(Gap.research_id == rid)).all()


# ruleid: sqlmodel-select-missing-deleted-at-filter
rows = session.exec(select(Note).where(Note.type == "raw")).all()


# ruleid: sqlmodel-select-missing-deleted-at-filter
rows = session.execute(select(Gap).where(Gap.term == term)).scalars().all()


# ok: deleted_at.is_(None) as direct arg to .where()
rows = session.exec(select(Gap).where(Gap.deleted_at.is_(None))).all()


# ok: deleted_at.is_(None) as direct arg to .where() — Note model
rows = session.exec(select(Note).where(Note.deleted_at.is_(None))).all()


# ok: deleted_at check as additional where arg alongside other conditions
rows = session.exec(
    select(Gap).where(Gap.research_id == rid, Gap.deleted_at.is_(None))
).all()


# ok: chained .where() calls — deleted_at in second clause
rows = session.exec(
    select(Note).where(Note.note_id == nid).where(Note.deleted_at.is_(None))
).all()


# ok: chained .where() with deleted_at first
rows = session.exec(
    select(Gap).where(Gap.deleted_at.is_(None)).where(Gap.state == "open")
).all()


# ok: equality check on deleted_at (unusual but valid)
rows = session.exec(select(Gap).where(Gap.deleted_at == None)).all()  # noqa: E711


# ok: select on a different model (not Gap or Note) — rule should not fire
rows = session.exec(select(Chunk)).all()


# ok: select on a different model (not Gap or Note)
rows = session.exec(select(Research).where(Research.state == "open")).all()
