1. In `projects/platform/authentik/factory-blueprints/factory-social.yaml`, the
   `reserve-email` policy takes a transaction-scoped advisory lock before the
   email lookup and insert. In `projects/platform/authentik/runtime/social.py`,
   `exercise_competing_enrollments` pauses the first insert before commit and
   completes the competing request on another database connection.
2. The same runtime fixture's `exercise` follows a single enrollment through
   the source callback and MFA, then counts authenticated sessions. Run
   `projects/platform/authentik/runtime/check.sh` on a Docker Compose host.
