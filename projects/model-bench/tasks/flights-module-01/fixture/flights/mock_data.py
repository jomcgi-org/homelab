"""Mock flight feed (stands in for a real upstream flight-status API).

Each record: flight_no, origin, dest (IATA codes), scheduled_dep and actual_dep as
ISO 8601 datetime strings, and status ("scheduled" or "cancelled"). A cancelled
flight has actual_dep = None.
"""

from __future__ import annotations

FLIGHTS: list[dict] = [
    {
        "flight_no": "BA100",
        "origin": "LHR",
        "dest": "JFK",
        "scheduled_dep": "2026-07-01T09:00:00",
        "actual_dep": "2026-07-01T09:05:00",
        "status": "scheduled",
    },
    {
        "flight_no": "BA101",
        "origin": "LHR",
        "dest": "CDG",
        "scheduled_dep": "2026-07-01T10:00:00",
        "actual_dep": "2026-07-01T10:30:00",
        "status": "scheduled",
    },
    {
        "flight_no": "BA102",
        "origin": "LHR",
        "dest": "AMS",
        "scheduled_dep": "2026-07-01T11:00:00",
        "actual_dep": None,
        "status": "cancelled",
    },
    {
        "flight_no": "U201",
        "origin": "EDI",
        "dest": "LHR",
        "scheduled_dep": "2026-07-01T08:00:00",
        "actual_dep": "2026-07-01T08:00:00",
        "status": "scheduled",
    },
    {
        "flight_no": "U202",
        "origin": "EDI",
        "dest": "DUB",
        "scheduled_dep": "2026-07-01T09:00:00",
        "actual_dep": "2026-07-01T09:20:00",
        "status": "scheduled",
    },
    {
        "flight_no": "FR301",
        "origin": "GLA",
        "dest": "STN",
        "scheduled_dep": "2026-07-01T07:00:00",
        "actual_dep": "2026-07-01T07:10:00",
        "status": "scheduled",
    },
]
