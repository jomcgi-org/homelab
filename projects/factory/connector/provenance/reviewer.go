package provenance

// ReviewerRun carries no prompt or issue text. The message ID makes retries
// idempotent; both normal code reviews and goal checks use this path.
type ReviewerRun struct {
	MessageID  string `json:"messageId"`
	Repository string `json:"repository"`
	Agent      string `json:"agent"`
	IssueKey   string `json:"issueKey"`
	Record
}
