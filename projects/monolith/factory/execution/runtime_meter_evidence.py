"""Read-only native metering proof shared with pruned receipt consumers.

No orchestration control, graph or stop machinery is imported here. Existing
execution transactions may validate terminal proof without acquiring new locks.
"""

import json

from sqlmodel import Session, select

from factory.execution import result_receipts
from factory.orchestration.factory_models import FactoryAudit

BOUND = "runtime_meter_bound"
UPDATE = "runtime_meter_usage"
TRIP = "runtime_meter_tripped"
COUNTERS = ("input_tokens", "output_tokens", "cached_input_tokens")


def binding(db, receipt_id):
    # Durable audit identity survives native receipt/body retention.
    records = records_for_receipt(db, receipt_id, BOUND)
    if len(records) != 1:
        raise result_receipts.ReceiptRejected(409, "meter_binding_missing")
    return records[0]


def records_for_receipt(db, receipt_id, action):
    query = select(FactoryAudit).where(
        FactoryAudit.action == action, FactoryAudit.detail_json.contains(receipt_id)
    )
    if action != BOUND:
        query = query.where(FactoryAudit.task_id == binding(db, receipt_id)["task_id"])
    return [
        {**json.loads(row.detail_json), "task_id": row.task_id}
        for row in db.exec(query.order_by(FactoryAudit.id)).all()
        if json.loads(row.detail_json).get("receipt_id") == receipt_id
    ]


def validate_completion(db, receipt_id, value):
    """Read durable proof only, without acquiring control or execution locks.

    Partial/error bodies remain available as evidence. Successful results need
    the exact invocation's final acknowledged counters, even after enrollment
    is disabled or when an older guest ignores the meter metadata.
    """
    bindings = records_for_receipt(db, receipt_id, BOUND)
    if not bindings:
        return
    if isinstance(value, dict) and value.get("terminal_reason") in {
        "error",
        "user_interrupt",
        "max_tokens",
        "tool_error",
    }:
        return
    updates = records_for_receipt(db, receipt_id, UPDATE)
    last = updates[-1] if updates else None
    usage = value.get("usage") if isinstance(value, dict) else None
    if (
        len(bindings) != 1
        or last is None
        or last["coverage"] != "complete"
        or records_for_receipt(db, receipt_id, TRIP)
        or not isinstance(usage, dict)
        or any(
            type(
                usage.get("cache_read_tokens" if key == "cached_input_tokens" else key)
            )
            is not int
            or usage.get("cache_read_tokens" if key == "cached_input_tokens" else key)
            != last["usage"][key]
            for key in COUNTERS
        )
    ):
        raise result_receipts.ReceiptRejected(409, "meter_terminal_usage_unverified")


def validate_response(receipt_id, value):
    with Session(result_receipts.get_engine()) as db:
        validate_completion(db, receipt_id, value)
