package detect

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"
)

// NoOffset is the value of Finding.Start and Finding.End for a detector that
// cannot map a finding to a byte offset of Input.Bytes.
const NoOffset = -1

// ErrBudget is returned by a detector that stopped because its own work limit
// was reached (for example, too many matches that its allowlists suppress). The
// host treats it as a call without a verdict.
var ErrBudget = errors.New("detector work limit reached")

// Class says what a finding means for the request.
type Class string

const (
	// ClassBlock is a credential: the request is denied (in enforce mode).
	ClassBlock Class = "block"
	// ClassReport is an indicator: the host counts it and never denies for it.
	ClassReport Class = "report"
)

// Input is one text to scan, with where it came from. Bytes is the content and
// is not necessarily UTF-8. A detector must not keep Bytes after Detect returns.
type Input struct {
	Bytes []byte
	// Source says which part of the request this is: "address", "body" or the
	// name the host gives. It is for metrics and logs, never a secret.
	Source string
	// Filename may be empty. It is the name of the content, for a detector that
	// reads files (a path rule); an HTTP body has none.
	Filename string
	// Via lists the decoders already applied to produce this text, in order
	// (for example "json"). Empty for the original bytes.
	Via []string
}

// Text returns Bytes as a string.
func (in Input) Text() string { return string(in.Bytes) }

// Finding is one match. It never holds the matched text.
type Finding struct {
	Detector string
	RuleID   string
	Class    Class
	// Start and End are byte offsets into Input.Bytes, or NoOffset.
	Start, End int
}

// Detector scans one Input. See the package documentation for the contract.
type Detector interface {
	Name() string
	Detect(ctx context.Context, in Input) ([]Finding, error)
}

// PrivateData is implemented by a detector that holds private data, such as a
// fingerprint index kept outside the public repository.
type PrivateData interface {
	// Load reads the data from a mounted path. It is called once at start.
	Load(path string) error
	// Reload reads the data again from the path of Load. A failed Reload keeps
	// the previous data.
	Reload() error
	// Health returns nil while the data is loaded and fresh enough to use.
	Health() error
}

// Mode says whether a detector's verdict is enforced.
type Mode string

const (
	// ModeEnforce lets the verdict deny a request.
	ModeEnforce Mode = "enforce"
	// ModeObserve counts the verdict and never denies.
	ModeObserve Mode = "observe"
)

// FailureMode says what a call without a verdict (timeout, error, budget,
// oversize input, panic) does in enforce mode.
type FailureMode string

const (
	// FailDeny denies the request.
	FailDeny FailureMode = "deny"
	// FailAllow lets the request through.
	FailAllow FailureMode = "allow"
)

// Limits bound one detector call. The host enforces both.
type Limits struct {
	// MaxInputBytes is the largest Input.Bytes a call may have. Larger input is a
	// failure, never a skip: padding must not bypass a detector. Zero: no limit.
	MaxInputBytes int
	// Deadline bounds all the calls for one request. Zero: no deadline.
	Deadline time.Duration
}

// Config selects and configures one detector.
type Config struct {
	Name        string
	Mode        Mode
	FailureMode FailureMode
	Limits      Limits
	// DataPath is the mounted path of the private data, for a PrivateData
	// detector. Empty for the others.
	DataPath string
	// Options holds detector-specific settings, such as "rules" for an operator
	// rule file. The host does not read them.
	Options map[string]string
}

// Validate reports a bad Config.
func (c Config) Validate() error {
	if c.Name == "" {
		return errors.New("detector name is empty")
	}
	if c.Mode != ModeEnforce && c.Mode != ModeObserve {
		return fmt.Errorf("detector mode must be enforce or observe, not %q", c.Mode)
	}
	if c.FailureMode != FailDeny && c.FailureMode != FailAllow {
		return fmt.Errorf("detector failure mode must be deny or allow, not %q", c.FailureMode)
	}
	if c.Limits.MaxInputBytes < 0 || c.Limits.Deadline < 0 {
		return errors.New("detector limits must not be negative")
	}
	return nil
}

// Factory builds a detector from its Config.
type Factory func(Config) (Detector, error)

var (
	mu        sync.RWMutex
	factories = map[string]Factory{}
)

// Register adds a detector under a name. It panics on an empty or a repeated
// name, so a wrong build fails at start.
func Register(name string, f Factory) {
	if name == "" || f == nil {
		panic("detect: Register needs a name and a factory")
	}
	mu.Lock()
	defer mu.Unlock()
	if _, dup := factories[name]; dup {
		panic("detect: detector registered twice: " + name)
	}
	factories[name] = f
}

// Names lists the registered detectors, sorted.
func Names() []string {
	mu.RLock()
	defer mu.RUnlock()
	names := make([]string, 0, len(factories))
	for n := range factories {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

// New builds the detector that cfg names and wraps it in a Host. It loads the
// private data of a PrivateData detector from cfg.DataPath.
func New(cfg Config) (*Host, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	mu.RLock()
	f, ok := factories[cfg.Name]
	mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown detector %q (registered: %v)", cfg.Name, Names())
	}
	d, err := f(cfg)
	if err != nil {
		return nil, fmt.Errorf("detector %s: %w", cfg.Name, err)
	}
	if p, ok := d.(PrivateData); ok {
		if cfg.DataPath == "" {
			return nil, fmt.Errorf("detector %s holds private data and needs a data path", cfg.Name)
		}
		if err := p.Load(cfg.DataPath); err != nil {
			return nil, fmt.Errorf("detector %s: load data: %w", cfg.Name, err)
		}
	}
	return NewHost(d, cfg), nil
}
