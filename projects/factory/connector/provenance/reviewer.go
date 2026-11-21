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

const (
	BindReviewerTaskPath = "/runs/reviewer/task"
	ReadReviewerPinPath  = "/runs/reviewer/task-pin"
)

// ReviewerTask identifies a native task and its owning reviewer binding.
// Only the dispatcher supplies the original message ID when it binds a task.
type ReviewerTask struct {
	Repository string `json:"repository"`
	Agent      string `json:"agent"`
	Session    string `json:"session"`
	Task       string `json:"task"`
	MessageID  string `json:"messageId,omitempty"`
}
