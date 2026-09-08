package server

import (
	"context"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"

	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

func strictDestroyServer(t *testing.T, drv *fakeDriver) (nodev1.NodeServiceClient, *Server, *nodev1.DestroyRequest) {
	t.Helper()
	client, srv := newSessionTestServer(t, drv, &fakeTransport{}, 8)
	srv.cfg.PodUID = "pod-proof"
	srv.sessionVMs.add(&sessionEntry{
		vmID: "vm-proof", sessionID: "s-proof", workload: "echo",
		handle: substrate.Handle{ID: "vm-proof", ThreadID: "proof"},
	})
	return client, srv, &nodev1.DestroyRequest{
		VmId: "vm-proof", OperationId: "op-proof",
		ExpectedBootId: srv.bootID, ExpectedSessionId: "s-proof",
	}
}

func strictContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func TestStrictDestroyPersistsBeforeRemovingAndReplaysAfterRestart(t *testing.T) {
	drv := &fakeDriver{live: 1}
	client, srv, req := strictDestroyServer(t, drv)
	ctx := strictContext(t)
	write := srv.destroyProofs.write
	srv.destroyProofs.write = func(path string, body []byte) error {
		if srv.sessionVMs.get(req.GetVmId()) == nil {
			t.Error("strict destroy removed identity before publishing proof")
		}
		if _, releases, _, _ := drv.counts(); releases != 1 {
			t.Errorf("proof publication before release: releases=%d", releases)
		}
		return write(path, body)
	}
	before := time.Now().UnixMilli()
	response, err := client.Destroy(ctx, req)
	if err != nil || !response.GetTeardownConfirmed() || response.GetCompletion() == nil {
		t.Fatalf("strict Destroy = %v, %v", response, err)
	}
	proof := response.GetCompletion()
	if proof.GetOperationId() != req.GetOperationId() || proof.GetVmId() != req.GetVmId() ||
		proof.GetBootId() != srv.bootID || proof.GetSessionId() != req.GetExpectedSessionId() ||
		proof.GetNode() != srv.cfg.Node || proof.GetPodUid() != srv.cfg.PodUID ||
		proof.GetCompletedAtUnixMs() < before || proof.GetCompletedAtUnixMs() > time.Now().UnixMilli() {
		t.Fatalf("wrong exact completion: %v", proof)
	}
	if srv.sessionVMs.get(req.GetVmId()) != nil {
		t.Fatal("successful strict destroy retained registry entry")
	}
	stored, err := readDestroyCompletion(srv.destroyProofs.path(req.GetOperationId()), true)
	if err != nil || !sameDestroyIdentity(stored, proof) || stored.GetCompletedAtUnixMs() != proof.GetCompletedAtUnixMs() {
		t.Fatalf("durable record = %v, %v", stored, err)
	}
	// Discard all process-local entries. The original pod can replay its old
	// completion even though the restarted daemon registers a fresh boot.
	restarted := New(Options{Config: srv.cfg, Driver: drv, SessionDriver: drv, Transport: &fakeTransport{}})
	if restarted.bootID == srv.bootID {
		t.Fatal("daemon restart reused boot identity")
	}
	replay, err := restarted.Destroy(ctx, req)
	if err != nil || !sameDestroyIdentity(replay.GetCompletion(), proof) ||
		replay.GetCompletion().GetCompletedAtUnixMs() != proof.GetCompletedAtUnixMs() {
		t.Fatalf("restart replay = %v, %v", replay, err)
	}
	if _, releases, bundles, _ := drv.counts(); releases != 1 || bundles != 1 {
		t.Fatalf("replay performed cleanup again: release=%d bundle=%d", releases, bundles)
	}
	// Durable records are outside both per-instance warmth and VM bundles.
	if filepath.Dir(srv.destroyProofs.root) != filepath.Dir(srv.cfg.SnapshotRoot) ||
		filepath.Base(srv.destroyProofs.root) != "destroy-completions" {
		t.Fatalf("completion store has unsafe placement %s", srv.destroyProofs.root)
	}
}

func TestStrictDestroyRejectsIncompleteAndChangedIdentityWithoutReap(t *testing.T) {
	for _, changed := range []string{"operation", "boot", "session", "vm", "missing-operation", "missing-boot", "missing-session", "missing-owner"} {
		t.Run(changed, func(t *testing.T) {
			drv := &fakeDriver{live: 1}
			client, srv, req := strictDestroyServer(t, drv)
			wantCode := codes.FailedPrecondition
			switch changed {
			case "operation":
				req.OperationId = "../../not-an-operation"
				wantCode = codes.InvalidArgument
			case "boot":
				req.ExpectedBootId = "boot-other"
			case "session":
				req.ExpectedSessionId = "s-other"
			case "vm":
				req.VmId = "vm-other"
			case "missing-operation":
				req.OperationId = ""
				wantCode = codes.InvalidArgument
			case "missing-boot":
				req.ExpectedBootId = ""
				wantCode = codes.InvalidArgument
			case "missing-session":
				req.ExpectedSessionId = ""
				wantCode = codes.InvalidArgument
			case "missing-owner":
				srv.cfg.PodUID = ""
			}
			response, err := client.Destroy(strictContext(t), req)
			if status.Code(err) != wantCode || response.GetCompletion() != nil {
				t.Fatalf("Destroy = %v, %v, want %v", response, err, wantCode)
			}
			if _, releases, bundles, _ := drv.counts(); releases != 0 || bundles != 0 {
				t.Fatalf("invalid identity changed VM: releases=%d bundles=%d", releases, bundles)
			}
			if e := srv.sessionVMs.get("vm-proof"); e == nil || e.teardown.started.Load() {
				t.Fatal("invalid strict identity fenced live session")
			}
		})
	}
}

func TestStrictDestroyRejectsConflictingCompletedOwner(t *testing.T) {
	for _, changed := range []string{"boot", "session", "vm", "node", "pod"} {
		t.Run(changed, func(t *testing.T) {
			drv := &fakeDriver{live: 1}
			client, srv, req := strictDestroyServer(t, drv)
			ctx := strictContext(t)
			if _, err := client.Destroy(ctx, req); err != nil {
				t.Fatal(err)
			}
			switch changed {
			case "boot":
				req.ExpectedBootId = "boot-other"
			case "session":
				req.ExpectedSessionId = "s-other"
			case "vm":
				req.VmId = "vm-other"
			case "node":
				srv.cfg.Node = "node-other"
			case "pod":
				srv.cfg.PodUID = "pod-other"
			}
			response, err := client.Destroy(ctx, req)
			if status.Code(err) != codes.FailedPrecondition || response.GetCompletion() != nil {
				t.Fatalf("conflicting replay = %v, %v", response, err)
			}
		})
	}
}

func TestStrictDestroyProofFailureRetainsIdentityForLegacyRetry(t *testing.T) {
	drv := &fakeDriver{live: 1}
	client, srv, req := strictDestroyServer(t, drv)
	ctx := strictContext(t)
	write := srv.destroyProofs.write
	srv.destroyProofs.write = func(string, []byte) error { return errors.New("injected proof write failure") }
	response, err := client.Destroy(ctx, req)
	if err == nil || response.GetCompletion() != nil {
		t.Fatalf("failed write affirmed completion: %v, %v", response, err)
	}
	if e := srv.sessionVMs.get(req.GetVmId()); e == nil || !e.teardown.started.Load() || e.teardown.done || !e.teardown.released {
		t.Fatal("failed publication discarded teardown progress")
	}
	// A different operation cannot take over the retained session or the same
	// operation a different VM, even while publication is still failing.
	conflict := &nodev1.DestroyRequest{VmId: req.GetVmId(), OperationId: "op-other", ExpectedBootId: srv.bootID, ExpectedSessionId: req.GetExpectedSessionId()}
	if _, err := client.Destroy(ctx, conflict); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("conflicting pending operation = %v", err)
	}
	if len(srv.destroyProofs.pending) != 1 {
		t.Fatalf("conflicts expanded pending identity memory: %d", len(srv.destroyProofs.pending))
	}
	srv.sessionVMs.add(&sessionEntry{vmID: "vm-other", sessionID: "s-other", handle: substrate.Handle{ID: "vm-other"}})
	conflict = &nodev1.DestroyRequest{VmId: "vm-other", OperationId: req.GetOperationId(), ExpectedBootId: srv.bootID, ExpectedSessionId: "s-other"}
	if _, err := client.Destroy(ctx, conflict); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("pending operation reused for another VM: %v", err)
	}
	legacy := &nodev1.DestroyRequest{VmId: req.GetVmId()}
	if response, err := client.Destroy(ctx, legacy); err == nil || response.GetCompletion() != nil {
		t.Fatalf("legacy retry bypassed pending proof: %v, %v", response, err)
	}
	srv.destroyProofs.write = write
	if response, err := client.Destroy(ctx, legacy); err != nil || !response.GetTeardownConfirmed() || response.GetCompletion() != nil {
		t.Fatalf("legacy retry after storage recovery = %v, %v", response, err)
	}
	if response, err := client.Destroy(ctx, req); err != nil || response.GetCompletion() == nil {
		t.Fatalf("strict replay after legacy cleanup = %v, %v", response, err)
	}
	if _, releases, _, _ := drv.counts(); releases != 1 {
		t.Fatalf("proof retry released twice: %d", releases)
	}
}

func TestStrictDestroyReleaseFailureAndMissingBootRecordNeverConfirm(t *testing.T) {
	drv := &fakeDriver{live: 1, failRelease: errors.New("still running")}
	client, srv, req := strictDestroyServer(t, drv)
	ctx := strictContext(t)
	if response, err := client.Destroy(ctx, req); err == nil || response.GetCompletion() != nil {
		t.Fatalf("release failure affirmed: %v, %v", response, err)
	}
	if proof, err := readDestroyCompletion(srv.destroyProofs.path(req.GetOperationId()), true); err != nil || proof != nil {
		t.Fatalf("failed reap wrote proof: %v, %v", proof, err)
	}
	restarted := New(Options{Config: srv.cfg, Driver: drv, SessionDriver: drv, Transport: &fakeTransport{}})
	if response, err := restarted.Destroy(ctx, req); status.Code(err) != codes.FailedPrecondition || response.GetCompletion() != nil {
		t.Fatalf("new boot converted missing VM to proof: %v, %v", response, err)
	}
	drv.mu.Lock()
	drv.failRelease = nil
	drv.mu.Unlock()
	if response, err := client.Destroy(ctx, req); err != nil || response.GetCompletion() == nil {
		t.Fatalf("original owner retry = %v, %v", response, err)
	}
}

func TestStrictAndLegacyDestroyCannotConfirmDuringReap(t *testing.T) {
	for _, firstStrict := range []bool{false, true} {
		t.Run(map[bool]string{false: "legacy-first", true: "strict-first"}[firstStrict], func(t *testing.T) {
			blocked := make(chan struct{})
			started := make(chan struct{}, 1)
			drv := &fakeDriver{live: 1, blockRelease: blocked, releaseStarted: started}
			client, srv, strict := strictDestroyServer(t, drv)
			var once sync.Once
			unblock := func() { once.Do(func() { close(blocked) }) }
			t.Cleanup(unblock)
			ctx := strictContext(t)
			legacy := &nodev1.DestroyRequest{VmId: strict.GetVmId()}
			first := legacy
			if firstStrict {
				first = strict
			}
			done := make(chan error, 1)
			go func() { _, err := client.Destroy(ctx, first); done <- err }()
			select {
			case <-started:
			case <-ctx.Done():
				t.Fatal("reap did not begin")
			}
			for _, request := range []*nodev1.DestroyRequest{strict, legacy} {
				if response, err := client.Destroy(ctx, request); err == nil || response.GetCompletion() != nil {
					t.Fatalf("concurrent request affirmed: %v, %v", response, err)
				}
			}
			if len(srv.sessionVMs.snapshot()) != 1 {
				t.Fatal("in-flight reap disappeared from inventory")
			}
			unblock()
			select {
			case err := <-done:
				if err != nil {
					t.Fatal(err)
				}
			case <-ctx.Done():
				t.Fatal("reap did not finish")
			}
			response, err := client.Destroy(ctx, strict)
			if firstStrict {
				if err != nil || response.GetCompletion() == nil {
					t.Fatalf("strict replay = %v, %v", response, err)
				}
			} else if status.Code(err) != codes.FailedPrecondition || response.GetCompletion() != nil {
				t.Fatalf("legacy absence became strict proof: %v, %v", response, err)
			}
		})
	}
}

func TestStrictDestroyStoreFullExpiryAndLegacyUnknown(t *testing.T) {
	drv := &fakeDriver{live: 1}
	client, srv, req := strictDestroyServer(t, drv)
	srv.destroyProofs.limit = 1
	ctx := strictContext(t)
	response, err := client.Destroy(ctx, req)
	if err != nil {
		t.Fatal(err)
	}
	completedAt := time.UnixMilli(response.GetCompletion().GetCompletedAtUnixMs())
	srv.sessionVMs.add(&sessionEntry{vmID: "vm-second", sessionID: "s-second", handle: substrate.Handle{ID: "vm-second"}})
	second := &nodev1.DestroyRequest{VmId: "vm-second", OperationId: "op-second", ExpectedBootId: srv.bootID, ExpectedSessionId: "s-second"}
	if response, err := client.Destroy(ctx, second); status.Code(err) != codes.ResourceExhausted || response.GetCompletion() != nil {
		t.Fatalf("full store accepted new strict stop: %v, %v", response, err)
	}
	if e := srv.sessionVMs.get("vm-second"); e == nil || e.teardown.started.Load() {
		t.Fatal("full store began destructive work")
	}
	// Exact retry still works at the limit, but expiration removes that proof's
	// availability and cannot turn the already-absent VM into new proof.
	if _, err := client.Destroy(ctx, req); err != nil {
		t.Fatal(err)
	}
	srv.destroyProofs.now = func() time.Time { return completedAt.Add(destroyCompletionRetention) }
	if response, err := client.Destroy(ctx, req); status.Code(err) != codes.FailedPrecondition || response.GetCompletion() != nil {
		t.Fatalf("expired record affirmed unknown VM: %v, %v", response, err)
	}
	for _, id := range []string{req.GetVmId(), "never-observed"} {
		response, err := client.Destroy(ctx, &nodev1.DestroyRequest{VmId: id})
		if err != nil || !response.GetTeardownConfirmed() || response.GetCompletion() != nil {
			t.Fatalf("legacy unknown behavior changed: %v, %v", response, err)
		}
	}
	if response, err := client.Destroy(ctx, second); err != nil || response.GetCompletion() == nil {
		t.Fatalf("expired inventory did not release storage capacity: %v, %v", response, err)
	}
	if _, err := os.Stat(srv.destroyProofs.path(req.GetOperationId())); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("expired completion retained after bounded prune: %v", err)
	}
}

func TestStrictDestroyCanceledRequestDoesNotStartAndRegisterSharesBoot(t *testing.T) {
	drv := &fakeDriver{live: 1}
	_, srv, req := strictDestroyServer(t, drv)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if response, err := srv.Destroy(ctx, req); status.Code(err) != codes.Canceled || response.GetCompletion() != nil {
		t.Fatalf("cancelled strict request = %v, %v", response, err)
	}
	if _, releases, _, _ := drv.counts(); releases != 0 {
		t.Fatal("cancelled request reaped VM")
	}
	srv.cfg.ControlPlaneURL = "http://cp.example"
	srv.cfg.ControlPlaneTokenPath = writeTempFile(t, "fake-token")
	doer := &fakeDoer{status: http.StatusOK}
	if err := srv.register(context.Background(), doer, srv.bootID); err != nil {
		t.Fatal(err)
	}
	recorded, ok := doer.last()
	if !ok || recorded.body.BootID != req.GetExpectedBootId() {
		t.Fatal("registration and Destroy boot differ")
	}
}

func TestDestroyCompletionAtomicPublicationAndCorruption(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "proof.json")
	body := []byte(`{"operation_id":"op-one","vm_id":"vm-one","boot_id":"boot-one","session_id":"s-one","completed_at_unix_ms":1,"node":"node-one","pod_uid":"pod-one"}`)
	if err := writeDestroyCompletion(path, body); err != nil {
		t.Fatal(err)
	}
	proof, err := readDestroyCompletion(path, true)
	if err != nil || proof.GetOperationId() != "op-one" {
		t.Fatalf("record = %v, %v", proof, err)
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 1 || entries[0].Name() != "proof.json" {
		t.Fatalf("temporary file leaked: %v, %v", entries, err)
	}
	for _, bad := range [][]byte{[]byte(`{broken`), append(append([]byte{}, body...), body...), make([]byte, maxDestroyCompletionBytes+1)} {
		if err := os.WriteFile(path, bad, 0o600); err != nil {
			t.Fatal(err)
		}
		if proof, err := readDestroyCompletion(path, true); err == nil || proof != nil {
			t.Fatalf("corrupt proof accepted: %v, %v", proof, err)
		}
	}
}

func TestStrictDestroyRefusesPrimedVMWithoutSessionIdentity(t *testing.T) {
	drv := &fakeDriver{live: 1}
	client, srv, req := strictDestroyServer(t, drv)
	e := srv.sessionVMs.remove(req.GetVmId())
	srv.vms.add(&vmEntry{id: e.vmID, workload: "echo", handle: e.handle})
	if response, err := client.Destroy(strictContext(t), req); status.Code(err) != codes.FailedPrecondition || response.GetCompletion() != nil {
		t.Fatalf("primed VM gained caller-supplied session identity: %v, %v", response, err)
	}
	if _, releases, _, _ := drv.counts(); releases != 0 {
		t.Fatal("strict request reaped unbound primed VM")
	}
	if len(srv.sessionVMs.snapshot()) != 0 {
		t.Fatal("strict request adopted primed VM")
	}
}

func TestStrictDestroyPublicationErrorRetriesDurabilityBeforeClearingInventory(t *testing.T) {
	drv := &fakeDriver{live: 1}
	client, srv, req := strictDestroyServer(t, drv)
	ctx := strictContext(t)
	write := srv.destroyProofs.write
	srv.destroyProofs.write = func(path string, body []byte) error {
		// Model a rename that became visible before its final fsync reported an
		// error. The next read must repeat the file and directory barriers.
		if err := write(path, body); err != nil {
			return err
		}
		return errors.New("publication acknowledgement lost")
	}
	if response, err := client.Destroy(ctx, req); err == nil || response.GetCompletion() != nil {
		t.Fatalf("failed publication was acknowledged: %v, %v", response, err)
	}
	if srv.sessionVMs.get(req.GetVmId()) == nil {
		t.Fatal("publication failure discarded identity")
	}
	srv.destroyProofs.write = write
	if response, err := client.Destroy(ctx, req); err != nil || response.GetCompletion() == nil {
		t.Fatalf("durable retry = %v, %v", response, err)
	}
	if srv.sessionVMs.get(req.GetVmId()) != nil {
		t.Fatal("durable replay retained stale inventory")
	}
	if _, releases, _, _ := drv.counts(); releases != 1 {
		t.Fatalf("retry re-released VM: %d", releases)
	}
}

func TestStrictDestroyStoreLockSerializesSeparateDaemonInstances(t *testing.T) {
	first := newDestroyCompletionStore(filepath.Join(t.TempDir(), "snapshots"))
	second := newDestroyCompletionStore(filepath.Join(filepath.Dir(first.root), "snapshots"))
	unlock, err := first.lock()
	if err != nil {
		t.Fatal(err)
	}
	defer unlock()
	if release, err := second.lock(); status.Code(err) != codes.Unavailable {
		if release != nil {
			release()
		}
		t.Fatalf("separate daemon bypassed node store lock: %v", err)
	}
}

func TestStrictDestroyLostClientDuringReapStillPublishesCompletion(t *testing.T) {
	blocked := make(chan struct{})
	started := make(chan struct{}, 1)
	published := make(chan struct{})
	drv := &fakeDriver{live: 1, blockRelease: blocked, releaseStarted: started}
	client, srv, req := strictDestroyServer(t, drv)
	var once sync.Once
	unblock := func() { once.Do(func() { close(blocked) }) }
	t.Cleanup(unblock)
	write := srv.destroyProofs.write
	srv.destroyProofs.write = func(path string, body []byte) error {
		if err := write(path, body); err != nil {
			return err
		}
		close(published)
		return nil
	}
	ctx, cancel := context.WithCancel(strictContext(t))
	defer cancel()
	done := make(chan error, 1)
	go func() { _, err := client.Destroy(ctx, req); done <- err }()
	select {
	case <-started:
	case <-ctx.Done():
		t.Fatal("strict reap did not start")
	}
	cancel()
	select {
	case err := <-done:
		if status.Code(err) != codes.Canceled {
			t.Fatalf("lost client = %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("cancelled client did not return")
	}
	if proof, err := readDestroyCompletion(srv.destroyProofs.path(req.GetOperationId()), true); err != nil || proof != nil {
		t.Fatalf("blocked reap produced proof: %v, %v", proof, err)
	}
	unblock()
	select {
	case <-published:
	case <-time.After(5 * time.Second):
		t.Fatal("completed reap lost proof with its HTTP caller")
	}
	proof, err := readDestroyCompletion(srv.destroyProofs.path(req.GetOperationId()), true)
	if err != nil || proof.GetBootId() != req.GetExpectedBootId() || proof.GetCompletedAtUnixMs() <= 0 {
		t.Fatalf("late completion = %v, %v", proof, err)
	}
}
