package main

import (
	"errors"
	"flag"
	"fmt"
	"os"
	"regexp"
	"time"
)

// config is the operator configuration. Flags carry all of it: the gate reads
// no secret from its environment. The only environment variable is POD_NAME,
// which the downward API sets.
type config struct {
	Listen      string
	AdminListen string
	PodName     string

	Atespace string
	Template string

	APIEndpoint   string
	APIServerName string
	APITokenFile  string
	RouterURL     string
	RouterName    string
	RouterCert    string
	CAFile        string

	TargetWaiting     int
	MaxInFlight       int
	MaxFallback       int
	CreateConcurrency int

	QueueWait            time.Duration
	DefaultDeadline      time.Duration
	MaxDeadline          time.Duration
	FallbackMinRemaining time.Duration
	RetryMinRemaining    time.Duration
	FailFast             time.Duration

	RecycleAfter time.Duration
	HardAfter    time.Duration

	APICallTimeout time.Duration
	SweepInterval  time.Duration
	SweepGrace     time.Duration
	ShutdownGrace  time.Duration
	DrainDelay     time.Duration

	MaxBody     int64
	MaxResponse int64
}

var (
	resourceName = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)
	// The longest name is eg-<pod>-<10 digit epoch>-<up to 8 digits>.
	maxPodNameLen = 63 - len("eg--") - 10 - len("-") - 8
)

func parseConfig(args []string) (config, error) {
	var c config
	fs := flag.NewFlagSet("engine-gate", flag.ContinueOnError)
	fs.StringVar(&c.Listen, "listen", ":8080", "address of the scan endpoint")
	fs.StringVar(&c.AdminListen, "admin-listen", ":9090", "address of /healthz, /readyz and /metrics")
	fs.StringVar(&c.PodName, "pod-name", os.Getenv("POD_NAME"), "pod name, the prefix of the children (default $POD_NAME)")
	fs.StringVar(&c.Atespace, "atespace", "egress-scan-engine", "atespace of the engine template and its children")
	fs.StringVar(&c.Template, "template", "engine", "fork template of the children (a name, never from a caller)")
	fs.StringVar(&c.APIEndpoint, "api-endpoint", "dns:///api.ate-system.svc:443", "ate API gRPC target")
	fs.StringVar(&c.APIServerName, "api-server-name", "api.ate-system.svc", "name on the ate API serving certificate")
	fs.StringVar(&c.APITokenFile, "api-token-file", "/var/run/ate/token", "projected ServiceAccount token, audience api.ate-system.svc")
	fs.StringVar(&c.RouterURL, "router-url", "https://atenet-router.ate-system.svc:443", "atenet-router HTTPS listener")
	fs.StringVar(&c.RouterName, "router-server-name", "atenet-router.ate-system.svc", "name on the router serving certificate")
	fs.StringVar(&c.RouterCert, "router-cert-file", "/var/run/router/credential-bundle.pem", "pod certificate bundle (client certificate and key)")
	fs.StringVar(&c.CAFile, "ca-file", "/var/run/router-ca/trust-bundle.pem", "trust bundle for the ate API and router serving certificates")
	fs.IntVar(&c.TargetWaiting, "target-waiting", 2, "waiting children to keep")
	fs.IntVar(&c.MaxInFlight, "max-inflight", 4, "scans at once")
	fs.IntVar(&c.MaxFallback, "max-fallback", 2, "children created on the request path at once (path A)")
	fs.IntVar(&c.CreateConcurrency, "create-concurrency", 2, "children created in the background at once")
	fs.DurationVar(&c.QueueWait, "queue-wait", 25*time.Millisecond, "wait for a free scan slot before shedding")
	fs.DurationVar(&c.DefaultDeadline, "default-deadline", 300*time.Millisecond, "deadline when the caller sends none")
	fs.DurationVar(&c.MaxDeadline, "max-deadline", 5*time.Second, "largest deadline a caller may ask for")
	fs.DurationVar(&c.FallbackMinRemaining, "fallback-min-remaining", 200*time.Millisecond, "time left that path A needs")
	fs.DurationVar(&c.RetryMinRemaining, "retry-min-remaining", 120*time.Millisecond, "time left that a retry on the next child needs")
	fs.DurationVar(&c.FailFast, "fail-fast", 5*time.Second, "after a failed create, refuse path A for this long")
	fs.DurationVar(&c.RecycleAfter, "recycle-after", 600*time.Second, "replace a waiting child this old")
	fs.DurationVar(&c.HardAfter, "hard-after", 780*time.Second, "never claim a child this old (the template max lifetime is 900 s)")
	fs.DurationVar(&c.APICallTimeout, "api-call-timeout", 10*time.Second, "timeout of one background ate API call")
	fs.DurationVar(&c.SweepInterval, "sweep-interval", 15*time.Second, "interval of the orphan sweep and the API probe")
	fs.DurationVar(&c.SweepGrace, "sweep-grace", 60*time.Second, "a served child is never swept before this age")
	fs.DurationVar(&c.ShutdownGrace, "shutdown-grace", 10*time.Second, "time to finish requests and delete the pool")
	fs.DurationVar(&c.DrainDelay, "drain-delay", 5*time.Second, "time between /readyz failing and the listener closing, on SIGTERM")
	// The tier sends at most about 1.1 MiB (the text it builds for Gitleaks).
	fs.Int64Var(&c.MaxBody, "max-body", 1258291, "largest request body in bytes (1.2 MiB)")
	fs.Int64Var(&c.MaxResponse, "max-response", 1<<20, "largest engine response in bytes")
	if err := fs.Parse(args); err != nil {
		return config{}, err
	}
	return c, c.validate()
}

func (c config) validate() error {
	if !resourceName.MatchString(c.PodName) || len(c.PodName) > maxPodNameLen {
		return fmt.Errorf("pod name %q must be a DNS label of at most %d characters", c.PodName, maxPodNameLen)
	}
	for name, v := range map[string]string{"atespace": c.Atespace, "template": c.Template} {
		if !resourceName.MatchString(v) || len(v) > 63 {
			return fmt.Errorf("%s %q must be a DNS label", name, v)
		}
	}
	for name, v := range map[string]string{
		"api-endpoint": c.APIEndpoint, "api-server-name": c.APIServerName, "api-token-file": c.APITokenFile,
		"router-url": c.RouterURL, "router-server-name": c.RouterName, "router-cert-file": c.RouterCert, "ca-file": c.CAFile,
	} {
		if v == "" {
			return fmt.Errorf("%s is required", name)
		}
	}
	switch {
	case c.TargetWaiting < 1 || c.TargetWaiting > 16:
		return errors.New("target-waiting must be between 1 and 16")
	case c.MaxInFlight < 1 || c.MaxInFlight > 64:
		return errors.New("max-inflight must be between 1 and 64")
	case c.MaxFallback < 0 || c.MaxFallback > c.MaxInFlight:
		return errors.New("max-fallback must be between 0 and max-inflight")
	case c.CreateConcurrency < 1 || c.CreateConcurrency > 8:
		return errors.New("create-concurrency must be between 1 and 8")
	case c.DefaultDeadline < 10*time.Millisecond || c.MaxDeadline < c.DefaultDeadline || c.MaxDeadline > time.Minute:
		return errors.New("deadlines must satisfy 10ms <= default-deadline <= max-deadline <= 1m")
	case c.QueueWait < 0 || c.QueueWait > time.Second:
		return errors.New("queue-wait must be between 0 and 1s")
	case c.RecycleAfter < 10*time.Second || c.HardAfter < c.RecycleAfter:
		return errors.New("recycle-after must be at least 10s and at most hard-after")
	case c.APICallTimeout < time.Second || c.SweepInterval < time.Second || c.SweepGrace < time.Second:
		return errors.New("api-call-timeout, sweep-interval and sweep-grace must be at least 1s")
	case c.DrainDelay < 0 || c.DrainDelay > 30*time.Second:
		return errors.New("drain-delay must be between 0 and 30s")
	case c.MaxBody < 1 || c.MaxBody > 16<<20:
		return errors.New("max-body must be between 1 and 16 MiB")
	case c.MaxResponse < 1<<10 || c.MaxResponse > 16<<20:
		return errors.New("max-response must be between 1 KiB and 16 MiB")
	}
	return nil
}

// prefix is the name prefix of the children of this pod.
func (c config) prefix() string { return "eg-" + c.PodName + "-" }
