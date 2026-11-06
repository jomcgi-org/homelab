"""Closed vocabulary, usable only alongside native positive not-invoked proof."""


def database_failure(value):
    if not isinstance(value, dict) or value.get("attempted") is not False:
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
