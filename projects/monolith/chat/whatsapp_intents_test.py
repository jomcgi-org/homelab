"""Tests for chat.whatsapp_intents.classify_intent (keyword routing)."""

from chat.whatsapp_intents import classify_intent


def test_record_prefix_and_phrases():
    assert classify_intent("record: we hiked Garibaldi") == "record"
    assert classify_intent("note that the boiler was serviced") == "record"
    assert classify_intent("for the record, we paid the deposit") == "record"


def test_reminder_beats_event_verb():
    # Names both a reminder verb and an event verb ("book"); reminder wins.
    assert classify_intent("remind us to book the table friday 7pm") == "reminder"
    assert classify_intent("don't forget the bins") == "reminder"


def test_schedule_explicit_and_verb_plus_time():
    assert classify_intent("add dinner with Sam Friday 7pm") == "schedule"
    assert classify_intent("schedule a call") == "schedule"
    assert classify_intent("put it on the calendar") == "schedule"


def test_bare_verb_without_time_is_not_schedule():
    # "put the bins out" has an event-ish verb but no time token: not scheduling.
    assert classify_intent("put the bins out") == "none"


def test_plain_conversation_is_none():
    assert classify_intent("what's the ferry plan?") == "none"
    assert classify_intent("") == "none"
    # A record phrasing without the prefix/keyword is not a capture request.
    assert classify_intent("booked the cabin for August") == "none"
