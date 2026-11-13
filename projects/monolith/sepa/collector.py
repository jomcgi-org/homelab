"""Bounded synchronous KiWIS job. See ARCHITECTURE.md for contract and terms."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx

ENDPOINT = "https://timeseries.sepa.org.uk/KiWIS/KiWIS"
SERIES_FIELDS = "station_id,station_no,station_name,ts_id,ts_path,ts_name,stationparameter_no,stationparameter_name,ts_unitsymbol"
STATION_FIELDS = "station_id,station_no,station_name,river_name"
VALUE_METADATA = "ts_id,ts_path,station_id,station_no,station_name,stationparameter_no,stationparameter_name,ts_unitsymbol"
MAX_BYTES = 1_048_576
logger = logging.getLogger("monolith.sepa")


@dataclass(frozen=True)
class Station:
    station_id: str
    station_no: str
    name: str
    river: str
    series_id: str

    @property
    def path(self) -> str:
        return f"1/{self.station_no}/SG/15m.Cmd"

    @property
    def metadata(self) -> dict:
        return dict(
            ts_id=self.series_id,
            ts_path=self.path,
            station_id=self.station_id,
            station_no=self.station_no,
            station_name=self.name,
            stationparameter_no="SG",
            stationparameter_name="Level",
            ts_unitsymbol="m",
        )


# Verified public metadata, 2026-11-13; station_no is NOT station_id.
STATIONS = (
    Station("36354", "133171", "Newton Stewart", "Cree", "54076010"),
    Station("38496", "9514", "Norham", "Tweed", "61557010"),
)


def configured_stations(value: str) -> tuple[Station, ...]:
    numbers = value.split(",")
    chosen = tuple(s for s in STATIONS if s.station_no in numbers)
    if not chosen or len(chosen) != len(numbers):
        raise ValueError(
            "SEPA_STATION_NUMBERS must be a unique subset of the verified catalog"
        )
    return chosen


def table_rows(payload: object) -> list[dict]:
    # format=json metadata is a header array followed by row arrays, NOT objects.
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        raise ValueError("invalid KiWIS metadata table")
    header = payload[0]
    if not all(isinstance(key, str) for key in header) or len(set(header)) != len(
        header
    ):
        raise ValueError("invalid KiWIS metadata header")
    rows = []
    for row in payload[1:]:
        if not isinstance(row, list) or len(row) != len(header):
            raise ValueError("partial KiWIS metadata row")
        rows.append(dict(zip(header, row, strict=True)))
    return rows


def validate_catalog(
    stations: tuple[Station, ...], series: object, sites: object
) -> None:
    series_rows, site_rows = table_rows(series), table_rows(sites)
    if len(series_rows) != len(stations) or len(site_rows) != len(stations):
        raise ValueError("incomplete or ambiguous SEPA catalog")
    for station in stations:
        expected = station.metadata | {"ts_name": "15minute"}
        matches = [r for r in series_rows if r.get("ts_id") == station.series_id]
        if len(matches) != 1 or any(
            matches[0].get(k) != v for k, v in expected.items()
        ):
            raise ValueError("SEPA stage metadata changed")
        sites_matching = [
            r for r in site_rows if r.get("station_id") == station.station_id
        ]
        expected_site = dict(
            station_id=station.station_id,
            station_no=station.station_no,
            station_name=station.name,
            river_name=station.river,
        )
        if len(sites_matching) != 1 or sites_matching[0] != expected_site:
            raise ValueError("SEPA station metadata changed")


def utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("SEPA timestamp has no timezone")
    return result.astimezone(timezone.utc)


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def parse_values(
    payload: object,
    station: Station,
    start: datetime,
    end: datetime,
    ingested_at: datetime,
) -> list[dict]:
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise ValueError("missing or ambiguous KiWIS series")
    item = payload[0]
    if any(item.get(k) != v for k, v in station.metadata.items()):
        raise ValueError("SEPA reading metadata changed")
    columns = item.get("columns", "").split(",")
    if len(columns) != 3 or set(columns) != {"Timestamp", "Value", "Quality Code"}:
        raise ValueError("unexpected KiWIS columns")
    data = item.get("data")
    if (
        not isinstance(data, list)
        or not 1 <= len(data) <= 97
        or str(len(data)) != str(item.get("rows"))
    ):
        raise ValueError("empty, oversized or partial KiWIS series")
    records, seen = [], set()
    for values in data:
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError("malformed KiWIS reading")
        row = dict(zip(columns, values, strict=True))
        observed = utc(row["Timestamp"])
        if (
            not start <= observed <= end
            or observed in seen
            or observed.second
            or observed.microsecond
            or observed.minute % 15
        ):
            raise ValueError("out of range, duplicate or non-15-minute timestamp")
        seen.add(observed)
        value, quality = row["Value"], row["Quality Code"]
        if value is not None and (
            type(value) not in (float, int) or not math.isfinite(value)
        ):
            raise ValueError("invalid SEPA stage value")
        if type(quality) is not int or not 0 <= quality <= 255:
            raise ValueError("missing or invalid SEPA quality")
        record = dict(
            schema_version=1,
            source="SEPA KiWIS",
            station_id=station.station_id,
            station_no=station.station_no,
            station_name=station.name,
            river=station.river,
            series_id=station.series_id,
            series_path=station.path,
            parameter="SG",
            units="m",
            observed_at=observed.isoformat(),
            value=float(value) if value is not None else None,
            quality_code=quality,
            provisional=quality == 140 if quality in (50, 100, 140, 150) else None,
            revision_possible=True,
            missing=value is None,
        )
        record["revision_id"] = fingerprint(record)
        record["ingested_at"] = ingested_at.astimezone(timezone.utc).isoformat()
        records.append(record)
    return sorted(records, key=lambda r: r["observed_at"])


class KiWIS:
    """Four logical requests maximum, each with at most one bounded retry."""

    def __init__(self, client: httpx.Client, sleep=time.sleep):
        self.client, self.sleep, self.requests = client, sleep, 0

    def get(self, request: str, **params) -> object:
        for attempt in range(2):
            if self.requests >= 8:
                raise RuntimeError("SEPA request budget exhausted")
            self.requests += 1
            try:
                with self.client.stream(
                    "GET",
                    ENDPOINT,
                    params=dict(
                        service="kisters",
                        type="queryServices",
                        datasource="0",
                        format="json",
                        request=request,
                        **params,
                    ),
                ) as response:
                    if (
                        response.status_code == 429
                        or 500 <= response.status_code <= 599
                    ):
                        delay = response.headers.get("Retry-After", "2")
                        # Do not sleep through daily quota exhaustion or HTTP-date delays.
                        if attempt or not delay.isdecimal() or int(delay) > 5:
                            raise RuntimeError("SEPA unavailable or rate limited")
                        self.sleep(max(1, int(delay)))
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise ValueError("SEPA response exceeds byte budget")
                    payload = json.loads(body)
                    if isinstance(payload, dict) and payload.get("type") == "error":
                        raise ValueError("KiWIS application error")
                    return payload
            except httpx.TransportError:
                if attempt:
                    raise
                self.sleep(2)
        raise RuntimeError("SEPA retry budget exhausted")


def object_batches(
    records: list[dict], prefix: str
) -> list[tuple[str, str, list[dict]]]:
    if not re.fullmatch(r"collectors/sepa(?:/[a-z0-9_-]+)*", prefix):
        raise ValueError("SEPA_PREFIX must be a dedicated collectors/sepa prefix")
    groups = defaultdict(list)
    for record in records:
        groups[
            (utc(record["observed_at"]).date().isoformat(), record["station_no"])
        ].append(record)
    result = []
    for (date, station), rows in sorted(groups.items()):
        rows.sort(key=lambda r: r["observed_at"])
        digest = fingerprint([r["revision_id"] for r in rows])
        result.append(
            (
                f"{prefix}/v1/date={date}/station={station}/{digest}.parquet",
                digest,
                rows,
            )
        )
    return result


def parquet_bytes(rows: list[dict]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    fields = [
        (key, pa.string())
        for key in (
            "source",
            "station_id",
            "station_no",
            "station_name",
            "river",
            "series_id",
            "series_path",
            "parameter",
            "units",
            "revision_id",
        )
    ]
    schema = pa.schema(
        fields
        + [
            ("schema_version", pa.int32()),
            ("observed_at", pa.timestamp("us", tz="UTC")),
            ("ingested_at", pa.timestamp("us", tz="UTC")),
            ("value", pa.float64()),
            ("quality_code", pa.int32()),
            ("provisional", pa.bool_()),
            ("revision_possible", pa.bool_()),
            ("missing", pa.bool_()),
        ],
        metadata={
            b"attribution": b"Contains SEPA data, licensed under the Open Government Licence v3.0."
        },
    )
    converted = [
        r | {"observed_at": utc(r["observed_at"]), "ingested_at": utc(r["ingested_at"])}
        for r in rows
    ]
    output = BytesIO()
    pq.write_table(
        pa.Table.from_pylist(converted, schema=schema), output, compression="zstd"
    )
    return output.getvalue()


def write_batches(s3, bucket: str, records: list[dict], prefix: str) -> tuple[int, int]:
    from botocore.exceptions import ClientError

    if (
        "IfNoneMatch"
        not in s3.meta.service_model.operation_model("PutObject").input_shape.members
    ):
        raise RuntimeError("pinned S3 client lacks conditional PutObject")
    created = duplicates = 0
    for key, digest, rows in object_batches(records, prefix):
        body = parquet_bytes(rows)
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/vnd.apache.parquet",
                Metadata={"sepa-content-sha256": digest, "schema-version": "1"},
                IfNoneMatch="*",
            )
            created += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "PreconditionFailed":
                raise  # Includes 409, unsupported conditions and authorization errors.
            existing = s3.head_object(Bucket=bucket, Key=key)
            if existing.get("Metadata") != {
                "sepa-content-sha256": digest,
                "schema-version": "1",
            }:
                raise RuntimeError("SEPA immutable object conflict") from None
            duplicates += 1  # Preserve first successful ingestion time and bytes.
    return created, duplicates


def run() -> None:
    # Also gate the handler: direct callers must not bypass the CLI guard.
    if os.environ.get("SEPA_ENABLED", "false") != "true":
        logger.info("SEPA disabled; requests=0 objects=0")
        return
    from botocore.config import Config
    from core.s3 import path_style_client

    stations = configured_stations(
        os.environ.get("SEPA_STATION_NUMBERS", "133171,9514")
    )
    prefix = os.environ.get("SEPA_PREFIX", "collectors/sepa")
    object_batches([], prefix)  # Validate before any network or credential/client use.
    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "")
    if not re.fullmatch(r"https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com", endpoint):
        raise ValueError("SEPA requires the established R2 conditional-write backend")
    bucket = os.environ.get("STARS_GRID_S3_BUCKET", "")
    if (
        not bucket
        or not os.environ.get("S3_ACCESS_KEY_ID")
        or not os.environ.get("S3_SECRET_ACCESS_KEY")
    ):
        raise ValueError("SEPA existing storage configuration is incomplete")
    now = datetime.now(timezone.utc)
    end = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    start = end - timedelta(days=1)
    numbers = ",".join(s.station_no for s in stations)
    with httpx.Client(
        timeout=10, follow_redirects=False, limits=httpx.Limits(max_connections=1)
    ) as client:
        api = KiWIS(client)
        series = api.get(
            "getTimeseriesList",
            station_no=numbers,
            stationparameter_no="SG",
            ts_shortname="15m.Cmd",
            returnfields=SERIES_FIELDS,
        )
        sites = api.get(
            "getStationList", station_no=numbers, returnfields=STATION_FIELDS
        )
        validate_catalog(stations, series, sites)
        records = []
        for station in stations:
            payload = api.get(
                "getTimeseriesValues",
                ts_id=station.series_id,
                **{"from": start.isoformat(), "to": end.isoformat()},
                returnfields="Timestamp,Value,Quality Code",
                metadata="true",
                md_returnfields=VALUE_METADATA,
                timezone="UTC",
            )
            records.extend(parse_values(payload, station, start, end, now))
    # All remote reads validated before any object writes. Synchronous job, no event loop.
    s3 = path_style_client(
        endpoint,
        config=Config(
            connect_timeout=5,
            read_timeout=10,
            retries={"mode": "standard", "total_max_attempts": 2},
            request_checksum_calculation="when_required",
        ),
    )
    created, duplicates = write_batches(s3, bucket, records, prefix)
    logger.info(
        "SEPA complete stations=%d readings=%d missing=%d absent_slots=%d requests=%d created=%d duplicate_objects=%d",
        len(stations),
        len(records),
        sum(r["missing"] for r in records),
        len(stations) * 97 - len(records),
        api.requests,
        created,
        duplicates,
    )
