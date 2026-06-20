# Tests for datetime-now-naive-fallback rule.
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import datetime as dt


def bad_now_no_args():
    # ruleid: datetime-now-naive-fallback
    return datetime.now()


def bad_utcnow():
    # ruleid: datetime-now-naive-fallback
    return datetime.utcnow()


def bad_module_now_no_args():
    # ruleid: datetime-now-naive-fallback
    return dt.datetime.now()


def bad_module_utcnow():
    # ruleid: datetime-now-naive-fallback
    return dt.datetime.utcnow()


def ok_now_keyword_tz():
    # ok: tz keyword argument provided
    return datetime.now(tz=timezone.utc)


def ok_now_positional_tz():
    # ok: timezone passed as positional argument
    return datetime.now(timezone.utc)


def ok_now_zoneinfo():
    # ok: ZoneInfo passed as argument
    return datetime.now(ZoneInfo("UTC"))


def ok_now_variable_tz():
    # ok: any argument satisfies the tz requirement
    tz = timezone.utc
    return datetime.now(tz)


def ok_module_now_keyword_tz():
    # ok: tz keyword argument on fully-qualified call
    return dt.datetime.now(tz=timezone.utc)


def ok_module_now_positional_tz():
    # ok: timezone passed as positional argument on fully-qualified call
    return dt.datetime.now(timezone.utc)
