# Tests for temporal-schedule-missing-overlap-policy rule.
# Positive cases (violations) have `# ruleid:` on the line above the call.
# Negative cases (ok) have `# ok:` on the line above the call.
#
# Background: SchedulePolicy.overlap defaults to UNSPECIFIED when not set,
# which lets Temporal pick any policy — in practice this can allow concurrent
# runs. Watermark-advancing workflows (e.g. note-export) require SKIP so that
# a new tick never races an in-progress run. Always pass overlap= explicitly.

import temporalio.client
from temporalio.client import ScheduleOverlapPolicy, SchedulePolicy


# --- Violations (should be flagged) ---


# ruleid: temporal-schedule-missing-overlap-policy
temporalio.client.SchedulePolicy()

# ruleid: temporal-schedule-missing-overlap-policy
temporalio.client.SchedulePolicy(
    catchup_window=None,
)

# ruleid: temporal-schedule-missing-overlap-policy
SchedulePolicy()

# ruleid: temporal-schedule-missing-overlap-policy
SchedulePolicy(catchup_window=None)


# --- OK cases (should not be flagged) ---

# ok: explicit overlap= present on qualified form
temporalio.client.SchedulePolicy(
    overlap=temporalio.client.ScheduleOverlapPolicy.SKIP,
)

# ok: explicit overlap= with other kwargs on qualified form
temporalio.client.SchedulePolicy(
    overlap=temporalio.client.ScheduleOverlapPolicy.SKIP,
    catchup_window=None,
)

# ok: explicit overlap= on unqualified form
SchedulePolicy(overlap=ScheduleOverlapPolicy.BUFFER_ONE)

# ok: explicit overlap= with other kwargs on unqualified form
SchedulePolicy(
    overlap=ScheduleOverlapPolicy.SKIP,
    catchup_window=None,
)
