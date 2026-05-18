# Tests for sqlalchemy-ne-on-nullable-without-isnull rule.
from sqlalchemy import or_, select


# ruleid: sqlalchemy-ne-on-nullable-without-isnull
def bad_visibility_ne(session):
    return session.exec(select(Note).where(Note.visibility != "public")).all()


# ruleid: sqlalchemy-ne-on-nullable-without-isnull
def bad_status_ne(session):
    return session.exec(select(Task).where(Task.status != "done")).all()


# ruleid: sqlalchemy-ne-on-nullable-without-isnull
def bad_edge_type_ne(session):
    return session.exec(select(Edge).where(Edge.edge_type != "related")).all()


# ruleid: sqlalchemy-ne-on-nullable-without-isnull
def bad_gap_class_ne(session):
    return session.exec(select(Gap).where(Gap.gap_class != "known")).all()


# ruleid: sqlalchemy-ne-on-nullable-without-isnull
def bad_visibility_chained_where(session):
    return (
        session.exec(
            select(Note)
            .where(Note.author == "alice")
            .where(Note.visibility != "private")
        ).all()
    )


# ok: visibility wrapped with or_ including .is_(None)
def ok_visibility_with_isnull(session):
    return session.exec(
        select(Note).where(or_(Note.visibility != "public", Note.visibility.is_(None)))
    ).all()


# ok: status wrapped with or_ including .is_(None)
def ok_status_with_isnull(session):
    return session.exec(
        select(Task).where(or_(Task.status != "done", Task.status.is_(None)))
    ).all()


# ok: edge_type wrapped with or_ including .is_(None)
def ok_edge_type_with_isnull(session):
    return session.exec(
        select(Edge).where(or_(Edge.edge_type != "related", Edge.edge_type.is_(None)))
    ).all()


# ok: gap_class wrapped with or_ including .is_(None)
def ok_gap_class_with_isnull(session):
    return session.exec(
        select(Gap).where(or_(Gap.gap_class != "known", Gap.gap_class.is_(None)))
    ).all()


# ok: non-nullable field (id) — not in the known-nullable list
def ok_non_nullable_field(session):
    return session.exec(select(Note).where(Note.id != 0)).all()


# ok: equality check on a nullable field (== silently excludes NULLs too, but that
# is a separate concern — this rule only targets !=)
def ok_equality_check(session):
    return session.exec(select(Note).where(Note.visibility == "public")).all()
