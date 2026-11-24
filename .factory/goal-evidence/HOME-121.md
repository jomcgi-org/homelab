1. `TestSemgrepTriageAfterVerdict` in `//projects/factory/reviewer/review:review_test` checks the App comment ID against the API finding's external discussion ID, including two findings on one line. The same test covers a missing binding, a contradictory finding, a failed verdict and an unresolved hold.
2. The HTTP 503 case in that test preserves the completed verdict.
3. The HTTP success cases assert both outputs for a single ID and an expanded response containing IDs 1 and 999. The expanded response names both affected findings and identifies the requested ID. The service validates the IDs and count in the write response.
4. `test_semgrep_key_is_scoped_to_opted_in_reviewer_service` in `//projects/factory/chart:reviewer_test` renders the secret reference and checks its consumers.

The Semgrep integration sends an authenticated POST with explicit issue IDs and product. The generated API contract specifies the reason, note and returned IDs. Its limit documentation also warns that Semgrep may triage other IDs sharing a fingerprint on other branches. Even a limit of one permits that expansion. The Homelab deployment must keep writes disabled until the provider supports restricting the operation to one ID. [Semgrep integration](https://github.com/r2c-CSE/semgrep-utilities/blob/8bc4eb7667008218d2ed9c4656992b660037b1c0/integrations/linear/semgrep_to_jira.py#L308-L319) · [Request contract](https://github.com/sdks-io/apimatic-semgrep-python-sdk/blob/aaef3dd9d30dbd526cc14180c922145b286f2bd4/semgrepwebapp/models/bulk_triage_request.py) · [Response contract](https://github.com/sdks-io/apimatic-semgrep-python-sdk/blob/aaef3dd9d30dbd526cc14180c922145b286f2bd4/semgrepwebapp/models/bulk_triage_response.py)

To verify the deployment constraint without an API key, inspect the pinned SDK used above. The request model says Semgrep will “also triage findings with the same fingerprint on other branches.” The response lists affected IDs after the write. Checking that list cannot prevent additional updates. [Request model, lines 27–34](https://github.com/sdks-io/apimatic-semgrep-python-sdk/blob/aaef3dd9d30dbd526cc14180c922145b286f2bd4/semgrepwebapp/models/bulk_triage_request.py#L27-L34)

```sh
git clone https://github.com/sdks-io/apimatic-semgrep-python-sdk.git semgrep-sdk
git -C semgrep-sdk show aaef3dd9d30dbd526cc14180c922145b286f2bd4:semgrepwebapp/models/bulk_triage_request.py | sed -n '27,34p'
```
