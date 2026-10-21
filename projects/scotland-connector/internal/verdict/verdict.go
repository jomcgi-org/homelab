// Package verdict is the one-word answer every connector answer leads with:
// go, marginal, no, or unknown when the forecast cannot say yet.
package verdict

// Verdict is the answer to "is this on".
type Verdict string

const (
	Go       Verdict = "go"
	Marginal Verdict = "marginal"
	No       Verdict = "no"
	// Unknown means the hourly forecast does not reach far enough to say.
	Unknown Verdict = "unknown"
)

// Rank orders verdicts best first: go, marginal, unknown, no. An unknown day
// may yet be good, so it sorts above a known bad one.
func (v Verdict) Rank() int {
	switch v {
	case Go:
		return 0
	case Marginal:
		return 1
	case Unknown:
		return 2
	}
	return 3
}

// All lists the verdicts in rank order, for schemas and tests.
var All = []Verdict{Go, Marginal, Unknown, No}
