"""Closed vocabulary, usable only alongside native positive not-invoked proof."""

# Public repair context is selected by code, never by exception text, SQL,
# model output or a caller-supplied path. These are diagnostic owning paths,
# not additions to any standing landing grant.
STAGE_OWNER = {
    "receipt_admission": "admission",
    "receipt_prepare": "result_receipts",
    "prepared_admission": "admission",
    "receipt_promotion": "result_receipts",
    "promotion_admission": "admission",
}
REFILL_ROLES = {
    "backlog_refill_collect_1": "collector",
    "backlog_refill_review_1": "independent reviewer",
}


def database_failure(value):
    if not isinstance(value, dict) or value.get("attempted") is not False:
        return None
    if not isinstance(value.get("phase"), str) or not isinstance(
        value.get("sqlstate"), str
    ):
        return None
    if "version" in value and (
        type(value["version"]) is not int or value["version"] != 1
    ):
        return None
    if value.get("phase") not in {
        "receipt_admission",
        "receipt_prepare",
        "prepared_admission",
        "receipt_promotion",
        "promotion_admission",
    } or value.get("sqlstate") not in {
        "42703",
        "42P01",
        "40001",
        "40P01",
        "55P03",
        "53300",
        "57P01",
        "08006",
    }:
        return None
    return {
        "version": 1,
        "phase": value["phase"],
        "sqlstate": value["sqlstate"],
        "attempted": False,
    }


def repair_context(value, node_key):
    failure = database_failure(value)
    role = REFILL_ROLES.get(node_key) if isinstance(node_key, str) else None
    if failure is None or role is None:
        return None
    owner = "projects/monolith/factory/execution/" + STAGE_OWNER[failure["phase"]]
    return {
        "role": role,
        "stage": failure["phase"],
        "sqlstate": failure["sqlstate"],
        "owning_paths": [owner + ".py", owner + "_test.py"],
    }
