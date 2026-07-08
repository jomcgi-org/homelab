"""Stateless AIS batch persistence: read-back dedup + batched write.

Postgres is the single source of truth. There is no in-memory authoritative
set: for every batch we read the affected vessels' current rows back from
ships.latest_positions, run the pure dedup logic (ais.should_insert_position),
then write survivors to ships.positions (append-only history) and upsert
ships.latest_positions (serving table) and ships.vessels (metadata).

Cross-dialect note: unit tests run on SQLite (SQLModel.metadata.create_all)
while production is Postgres. The read-back uses the ORM select() so it is
clean on both dialects (no raw text() / psycopg3 ::text casts). Upserts use
session.merge() (LatestPosition) and get-or-update (Vessel), both of which are
ORM-level and work identically on SQLite and Postgres. At AIS volume (hundreds
of rows per batch) the per-row SELECT that merge() issues is negligible, and
keeping the snapshot reads a plain select(LatestPosition) is worth far more
than shaving a dialect-specific INSERT ... ON CONFLICT.
"""

from datetime import datetime, timezone

from sqlmodel import Session, select

from ships.ais import PriorPosition, should_insert_position
from ships.models import LatestPosition, Position, Vessel


def _as_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to tz-aware UTC.

    Postgres returns tz-aware datetimes, but SQLite (used in unit tests) returns
    naive ones even though we always write tz-aware UTC. Treat naive values as
    UTC so the dedup time math (new.recorded_at minus prior.recorded_at) never
    mixes aware and naive operands (which would raise TypeError and wrongly
    force an insert).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def persist_batch(session: Session, positions: list[dict], vessels: list[dict]) -> int:
    """Dedup + write one AIS batch. Returns count of position rows inserted.

    Stateless: prior state is read back from ships.latest_positions, never an
    in-memory cache. positions/vessels are dicts as produced by ais.parse_message.
    """
    if not positions and not vessels:
        return 0

    # Read back the current latest_positions rows for every mmsi in this batch.
    # This is the only source of prior state: no module-level cache.
    mmsis = list({p["mmsi"] for p in positions if p.get("mmsi")})
    prior_rows: dict[str, LatestPosition] = {}
    prior: dict[str, PriorPosition] = {}
    if mmsis:
        rows = session.exec(
            select(LatestPosition).where(LatestPosition.mmsi.in_(mmsis))
        ).all()
        for row in rows:
            prior_rows[row.mmsi] = row
            prior[row.mmsi] = PriorPosition(
                lat=row.lat,
                lon=row.lon,
                speed=row.speed,
                recorded_at=_as_utc(row.recorded_at),
                first_seen_at_location=_as_utc(row.first_seen_at_location),
            )

    history: list[Position] = []
    # Latest surviving position per mmsi within this batch, with computed first_seen.
    latest_by_mmsi: dict[str, tuple[dict, datetime | None]] = {}

    for data in positions:
        mmsi = data.get("mmsi")
        if not mmsi:
            continue

        should, first_seen = should_insert_position(data, prior.get(mmsi))
        if not should:
            continue

        history.append(
            Position(
                mmsi=mmsi,
                lat=data["lat"],
                lon=data["lon"],
                speed=data.get("speed"),
                course=data.get("course"),
                heading=data.get("heading"),
                nav_status=data.get("nav_status"),
                ship_name=data.get("ship_name") or None,
                recorded_at=data["recorded_at"],
                received_at=datetime.now(timezone.utc),
            )
        )
        latest_by_mmsi[mmsi] = (data, first_seen)

        # Advance the working prior so a SECOND position for the same mmsi later
        # in this batch dedups against the just-accepted state. This preserves
        # the old per-message cache semantics within a batch without any module
        # state (the update lives in this local dict and dies with the call).
        prior[mmsi] = PriorPosition(
            lat=data["lat"],
            lon=data["lon"],
            speed=data.get("speed"),
            recorded_at=data["recorded_at"],
            first_seen_at_location=first_seen,
        )

    # Append-only history insert.
    if history:
        session.add_all(history)

    # Upsert latest_positions (serving + dedup read-back table). Preserve the
    # existing ship_name when the new position carries none (mirror the old
    # COALESCE(excluded.ship_name, latest_positions.ship_name)).
    for mmsi, (data, first_seen) in latest_by_mmsi.items():
        prior_row = prior_rows.get(mmsi)
        ship_name = data.get("ship_name") or (
            prior_row.ship_name if prior_row else None
        )
        session.merge(
            LatestPosition(
                mmsi=mmsi,
                lat=data["lat"],
                lon=data["lon"],
                speed=data.get("speed"),
                course=data.get("course"),
                heading=data.get("heading"),
                nav_status=data.get("nav_status"),
                ship_name=ship_name,
                recorded_at=data["recorded_at"],
                first_seen_at_location=first_seen,
                updated_at=datetime.now(timezone.utc),
            )
        )

    # Upsert vessel metadata. Coalesce nullable fields against the existing row
    # when the new value is falsy (mirror the old COALESCE(excluded.x, vessels.x)).
    new_vessels: list[Vessel] = []
    for v in vessels:
        mmsi = v.get("mmsi")
        if not mmsi:
            continue

        existing = session.get(Vessel, mmsi)
        if existing is None:
            new_vessels.append(
                Vessel(
                    mmsi=mmsi,
                    imo=v.get("imo") or None,
                    call_sign=v.get("call_sign") or None,
                    name=v.get("name") or None,
                    ship_type=v.get("ship_type"),
                    dimension_a=v.get("dimension_a"),
                    dimension_b=v.get("dimension_b"),
                    dimension_c=v.get("dimension_c"),
                    dimension_d=v.get("dimension_d"),
                    destination=v.get("destination") or None,
                    eta=v.get("eta"),
                    draught=v.get("draught"),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            continue

        # Coalesce text fields on falsy (empty strings come from parse_message).
        existing.name = (v.get("name") or "").strip() or existing.name
        existing.imo = v.get("imo") or existing.imo
        existing.call_sign = (v.get("call_sign") or "").strip() or existing.call_sign
        existing.destination = (
            v.get("destination") or ""
        ).strip() or existing.destination
        # Coalesce structured fields when the new value is present (not None).
        if v.get("ship_type") is not None:
            existing.ship_type = v.get("ship_type")
        if v.get("dimension_a") is not None:
            existing.dimension_a = v.get("dimension_a")
        if v.get("dimension_b") is not None:
            existing.dimension_b = v.get("dimension_b")
        if v.get("dimension_c") is not None:
            existing.dimension_c = v.get("dimension_c")
        if v.get("dimension_d") is not None:
            existing.dimension_d = v.get("dimension_d")
        if v.get("eta") is not None:
            existing.eta = v.get("eta")
        if v.get("draught") is not None:
            existing.draught = v.get("draught")
        existing.updated_at = datetime.now(timezone.utc)
        # No session.add(existing) needed: rows fetched via session.get are
        # already tracked, so attribute mutations flush on commit. (Adding in a
        # loop also trips the session-add-in-loop semgrep rule.)

    if new_vessels:
        session.add_all(new_vessels)

    session.commit()
    return len(history)
