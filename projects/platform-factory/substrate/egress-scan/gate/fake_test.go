package main

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/gate/ateapi"
)

const testAtespace = "egress-scan-engine"

// fakeAPI is an in-memory ate control API. It speaks gRPC through bufconn, so
// the real client code (grpcAPI, the preview interceptor and the bearer token)
// runs against it.
type fakeAPI struct {
	ateapi.UnimplementedControlServer

	mu      sync.Mutex
	actors  map[string]*ateapi.Actor
	pageMax int

	createDelay, resumeDelay, deleteDelay atomic.Int64 // nanoseconds
	deleteActive, deleteMax               atomic.Int64
	deleteGate                            atomic.Pointer[chan struct{}] // a Delete waits for this channel to close
	// failure injection: a gRPC code per operation, OK means none
	failCreate, failResume, failList, failDelete atomic.Uint32
	down                                         atomic.Bool
	resumeSlow                                   atomic.Bool // Resume returns RESUMING, Get later returns RUNNING

	creates, resumes, deletes atomic.Int64
	previewMissing            atomic.Int64
	authMissing               atomic.Int64
	wrongAtespace             atomic.Int64
	wrongTemplate             atomic.Int64
	deleted                   []string
}

func newFakeAPI() *fakeAPI { return &fakeAPI{actors: map[string]*ateapi.Actor{}, pageMax: 2} }

func (f *fakeAPI) check(ctx context.Context) error {
	md, _ := metadata.FromIncomingContext(ctx)
	if v := md.Get(previewHeader); len(v) != 1 || v[0] != "true" {
		f.previewMissing.Add(1)
	}
	if v := md.Get("authorization"); len(v) != 1 || !strings.HasPrefix(v[0], "Bearer ") {
		f.authMissing.Add(1)
	}
	if f.down.Load() {
		return status.Error(codes.Unavailable, "down")
	}
	return nil
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	if d <= 0 {
		return nil
	}
	select {
	case <-time.After(d):
		return nil
	case <-ctx.Done():
		return status.FromContextError(ctx.Err()).Err()
	}
}

func (f *fakeAPI) add(name string, state ateapi.ActorState) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.actors[name] = &ateapi.Actor{
		Metadata: &ateapi.ResourceMetadata{Atespace: testAtespace, Name: name, CreateTime: timestamppb.Now()},
		Status:   &ateapi.ActorStatus{State: state},
	}
}

// exit models the control plane: the child served its request, exited, and the
// API deletes the actor.
func (f *fakeAPI) exit(name string) {
	f.mu.Lock()
	delete(f.actors, name)
	f.mu.Unlock()
}

func (f *fakeAPI) names(prefix string) []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for n := range f.actors {
		if strings.HasPrefix(n, prefix) {
			out = append(out, n)
		}
	}
	sort.Strings(out)
	return out
}

func (f *fakeAPI) running(prefix string) []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []string
	for n, a := range f.actors {
		if strings.HasPrefix(n, prefix) && a.GetStatus().GetState() == ateapi.ActorState_ACTOR_STATE_RUNNING {
			out = append(out, n)
		}
	}
	sort.Strings(out)
	return out
}

func (f *fakeAPI) CreateActor(ctx context.Context, req *ateapi.CreateActorRequest) (*ateapi.Actor, error) {
	if err := f.check(ctx); err != nil {
		return nil, err
	}
	f.creates.Add(1)
	if err := sleepCtx(ctx, time.Duration(f.createDelay.Load())); err != nil {
		return nil, err
	}
	if f.failCreate.Load() != 0 {
		return nil, status.Error(codes.Code(f.failCreate.Load()), "create failed")
	}
	a := req.GetActor()
	if a.GetMetadata().GetAtespace() != testAtespace {
		f.wrongAtespace.Add(1)
	}
	if a.GetActorTemplate().GetName() != "engine" || a.GetActorTemplate().GetAtespace() != testAtespace {
		f.wrongTemplate.Add(1)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	name := a.GetMetadata().GetName()
	if _, ok := f.actors[name]; ok {
		return nil, status.Error(codes.AlreadyExists, "exists")
	}
	out := &ateapi.Actor{
		Metadata:      &ateapi.ResourceMetadata{Atespace: testAtespace, Name: name, CreateTime: timestamppb.Now()},
		ActorTemplate: a.GetActorTemplate(),
		Status:        &ateapi.ActorStatus{State: ateapi.ActorState_ACTOR_STATE_SUSPENDED, Fork: &ateapi.ActorFork{Role: ateapi.ForkRole_FORK_ROLE_FORK}},
	}
	f.actors[name] = out
	return proto.Clone(out).(*ateapi.Actor), nil
}

func (f *fakeAPI) ResumeActor(ctx context.Context, req *ateapi.ResumeActorRequest) (*ateapi.ResumeActorResponse, error) {
	if err := f.check(ctx); err != nil {
		return nil, err
	}
	f.resumes.Add(1)
	if err := sleepCtx(ctx, time.Duration(f.resumeDelay.Load())); err != nil {
		return nil, err
	}
	if f.failResume.Load() != 0 {
		return nil, status.Error(codes.Code(f.failResume.Load()), "resume failed")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	a, ok := f.actors[req.GetActor().GetName()]
	if !ok {
		return nil, status.Error(codes.NotFound, "no actor")
	}
	if f.resumeSlow.Load() {
		a.Status.State = ateapi.ActorState_ACTOR_STATE_RESUMING
		go func(name string) {
			time.Sleep(30 * time.Millisecond)
			f.mu.Lock()
			if a, ok := f.actors[name]; ok {
				a.Status.State = ateapi.ActorState_ACTOR_STATE_RUNNING
			}
			f.mu.Unlock()
		}(a.GetMetadata().GetName())
		return &ateapi.ResumeActorResponse{Actor: proto.Clone(a).(*ateapi.Actor), Resumed: false}, nil
	}
	a.Status.State = ateapi.ActorState_ACTOR_STATE_RUNNING
	return &ateapi.ResumeActorResponse{Actor: proto.Clone(a).(*ateapi.Actor), Resumed: true}, nil
}

func (f *fakeAPI) GetActor(ctx context.Context, req *ateapi.GetActorRequest) (*ateapi.Actor, error) {
	if err := f.check(ctx); err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	a, ok := f.actors[req.GetActor().GetName()]
	if !ok {
		return nil, status.Error(codes.NotFound, "no actor")
	}
	return proto.Clone(a).(*ateapi.Actor), nil
}

func (f *fakeAPI) DeleteActor(ctx context.Context, req *ateapi.DeleteActorRequest) (*ateapi.Actor, error) {
	if err := f.check(ctx); err != nil {
		return nil, err
	}
	f.deletes.Add(1)
	active := f.deleteActive.Add(1)
	defer f.deleteActive.Add(-1)
	for {
		m := f.deleteMax.Load()
		if active <= m || f.deleteMax.CompareAndSwap(m, active) {
			break
		}
	}
	if err := sleepCtx(ctx, time.Duration(f.deleteDelay.Load())); err != nil {
		return nil, err
	}
	if g := f.deleteGate.Load(); g != nil {
		select {
		case <-*g:
		case <-ctx.Done():
			return nil, status.FromContextError(ctx.Err()).Err()
		}
	}
	if f.failDelete.Load() != 0 {
		return nil, status.Error(codes.Code(f.failDelete.Load()), "delete failed")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	name := req.GetActor().GetName()
	a, ok := f.actors[name]
	if !ok {
		return nil, status.Error(codes.NotFound, "no actor")
	}
	delete(f.actors, name)
	f.deleted = append(f.deleted, name)
	return a, nil // a is out of the map now: no one else holds it
}

func (f *fakeAPI) deletedNames() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.deleted...)
}

// ListActors pages at pageMax, so the client's paging is exercised.
func (f *fakeAPI) ListActors(ctx context.Context, req *ateapi.ListActorsRequest) (*ateapi.ListActorsResponse, error) {
	if err := f.check(ctx); err != nil {
		return nil, err
	}
	if f.failList.Load() != 0 {
		return nil, status.Error(codes.Code(f.failList.Load()), "list failed")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	var all []string
	for n := range f.actors {
		all = append(all, n)
	}
	sort.Strings(all)
	start := 0
	if tok := req.GetOptions().GetPageToken(); tok != "" {
		fmt.Sscanf(tok, "%d", &start)
	}
	end := min(start+f.pageMax, len(all))
	resp := &ateapi.ListActorsResponse{}
	for _, n := range all[start:end] {
		resp.Actors = append(resp.Actors, proto.Clone(f.actors[n]).(*ateapi.Actor))
	}
	if end < len(all) {
		resp.NextPageToken = fmt.Sprint(end)
	}
	return resp, nil
}

// fakeRouter stands in for the router and the child behind it.
type fakeRouter struct {
	mu    sync.Mutex
	sent  []sentBody
	delay time.Duration
	// fn overrides the answer. It sees the child name and the call number.
	fn func(ctx context.Context, child string, n int) (sendResult, error)
	// reply is the default engine answer.
	reply []byte
	calls atomic.Int64
}

type sentBody struct {
	Target string
	Body   []byte
}

var cleanReply = []byte(".\n{\"results\":[],\"errors\":[],\"paths\":{\"skipped\":[]}}\n")

func (r *fakeRouter) Send(ctx context.Context, target string, body []byte) (sendResult, error) {
	n := int(r.calls.Add(1))
	r.mu.Lock()
	r.sent = append(r.sent, sentBody{Target: target, Body: append([]byte(nil), body...)})
	r.mu.Unlock()
	if r.delay > 0 {
		select {
		case <-time.After(r.delay):
		case <-ctx.Done():
			return sendResult{}, &sendError{Kind: sendTimeout}
		}
	}
	if r.fn != nil {
		_, child, _ := strings.Cut(target, "/")
		return r.fn(ctx, child, n)
	}
	reply := r.reply
	if reply == nil {
		reply = cleanReply
	}
	return sendResult{Body: reply, Engine: time.Millisecond}, nil
}

func (r *fakeRouter) targets() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	var out []string
	for _, s := range r.sent {
		out = append(out, s.Target)
	}
	return out
}

// fakeClock is the real clock plus an offset that a test can advance.
type fakeClock struct {
	mu     sync.Mutex
	offset time.Duration
}

func (c *fakeClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return time.Now().Add(c.offset)
}

func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	c.offset += d
	c.mu.Unlock()
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

// env wires a gate to the fakes. Nothing runs until the test starts it.
type env struct {
	t     *testing.T
	cfg   config
	api   *fakeAPI
	rt    *fakeRouter
	m     *metrics
	pool  *pool
	gate  *gate
	clock *fakeClock
	logs  *syncBuffer

	cancel context.CancelFunc
	done   chan struct{}
}

func testConfig(t *testing.T) config {
	t.Helper()
	cfg, err := parseConfig([]string{"--pod-name=gate-0"})
	if err != nil {
		t.Fatal(err)
	}
	cfg.TargetWaiting = 2
	cfg.QueueWait = 25 * time.Millisecond
	cfg.DefaultDeadline = 2 * time.Second
	cfg.SweepInterval = time.Second
	cfg.APICallTimeout = 2 * time.Second
	return cfg
}

func newEnv(t *testing.T, mod func(*config)) *env {
	t.Helper()
	cfg := testConfig(t)
	if mod != nil {
		mod(&cfg)
	}
	if err := cfg.validate(); err != nil {
		t.Fatal(err)
	}
	api := newFakeAPI()
	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer()
	ateapi.RegisterControlServer(srv, api)
	go srv.Serve(lis)
	tokenFile := t.TempDir() + "/token"
	writeFile(t, tokenFile, "test-token\n")
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return lis.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithPerRPCCredentials(bearerFile{path: tokenFile}),
		grpc.WithChainUnaryInterceptor(previewInterceptor),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close(); srv.Stop() })

	logs := &syncBuffer{}
	lg := newLimitedLog(slog.New(slog.NewJSONHandler(logs, nil)))
	m := newMetrics()
	baseCtx, cancelBase := context.WithCancel(context.Background())
	t.Cleanup(cancelBase)
	p := newPool(baseCtx, cfg, newGRPCAPI(conn, cfg.Atespace, cfg.Template), m, lg)
	clock := &fakeClock{}
	p.now = clock.now
	rt := &fakeRouter{}
	return &env{t: t, cfg: cfg, api: api, rt: rt, m: m, pool: p, gate: newGate(cfg, p, rt, m, lg), clock: clock, logs: logs}
}

// start runs the pool loop and waits until the pool is full.
func (e *env) start() {
	e.t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	e.cancel, e.done = cancel, make(chan struct{})
	go func() { defer close(e.done); e.pool.run(ctx) }()
	e.t.Cleanup(e.stop)
	e.eventually("pool full", func() bool { return e.waitingCount() >= e.cfg.TargetWaiting })
}

func (e *env) stop() {
	if e.cancel != nil {
		e.cancel()
		<-e.done
		e.cancel = nil
	}
	e.pool.bg.Wait()
}

func (e *env) waitingCount() int {
	e.pool.mu.Lock()
	defer e.pool.mu.Unlock()
	return len(e.pool.waiting)
}

func (e *env) waitingNames() []string {
	e.pool.mu.Lock()
	defer e.pool.mu.Unlock()
	var out []string
	for _, c := range e.pool.waiting {
		out = append(out, c.name)
	}
	return out
}

func (e *env) eventually(what string, cond func() bool) {
	e.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	e.t.Fatalf("timed out waiting for: %s", what)
}

func writeFile(t testing.TB, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}
