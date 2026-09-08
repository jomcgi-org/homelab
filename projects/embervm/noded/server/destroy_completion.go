package server

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	nodev1 "github.com/jomcgi/homelab/projects/embervm/proto/embervm/node/v1"
)

const (
	// A full store refuses new strict stops before reap. Expiry never implies
	// cessation: a missing/expired record cannot confirm an unknown VM.
	destroyCompletionLimit     = 4096
	destroyCompletionRetention = 7 * 24 * time.Hour
	maxDestroyCompletionBytes  = 4096
)

var destroyIdentityPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)

// destroyCompletionStore lives beside SnapshotRoot, outside VM bundles and
// per-instance warmth GC. It survives a daemon process restart, not loss of the
// node's scratch filesystem. One nonblocking node-shared flock serializes rare
// strict stops, including reap, so conflicting writers cannot destroy first and
// discover their operation collision only when publishing the proof.
// pending retains same-process failed operations; a restart without committed
// proof rejects the old boot even when the physical VM is no longer known.
type destroyCompletionStore struct {
	mu      sync.Mutex
	root    string
	limit   int
	now     func() time.Time
	write   func(string, []byte) error
	pending map[string]*nodev1.DestroyCompletion // guarded by the store flock
}

func newDestroyCompletionStore(snapshotRoot string) *destroyCompletionStore {
	root := ""
	if snapshotRoot != "" {
		root = filepath.Join(filepath.Dir(snapshotRoot), "destroy-completions")
	}
	return &destroyCompletionStore{
		root: root, limit: destroyCompletionLimit, now: time.Now,
		write: writeDestroyCompletion, pending: make(map[string]*nodev1.DestroyCompletion),
	}
}

func syncDestroyDirectory(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func (p *destroyCompletionStore) lock() (func(), error) {
	if !p.mu.TryLock() {
		return nil, status.Error(codes.Unavailable, "noded: strict destroy store busy")
	}
	failed := true
	defer func() {
		if failed {
			p.mu.Unlock()
		}
	}()
	if p.root == "" {
		return nil, status.Error(codes.FailedPrecondition, "noded: strict destroy storage unavailable")
	}
	if err := os.Mkdir(p.root, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return nil, err
	}
	// Persist the store directory itself before using a file inside it as proof.
	if err := syncDestroyDirectory(filepath.Dir(p.root)); err != nil {
		return nil, err
	}
	lock, err := os.OpenFile(filepath.Join(p.root, ".lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := unix.Flock(int(lock.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		lock.Close()
		if errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN) {
			return nil, status.Error(codes.Unavailable, "noded: strict destroy store busy")
		}
		return nil, err
	}
	failed = false
	return func() {
		_ = unix.Flock(int(lock.Fd()), unix.LOCK_UN)
		_ = lock.Close()
		p.mu.Unlock()
	}, nil
}

func (p *destroyCompletionStore) path(operationID string) string {
	digest := sha256.Sum256([]byte(operationID))
	return filepath.Join(p.root, hex.EncodeToString(digest[:])+".json")
}

func readDestroyCompletion(path string, durable bool) (*nodev1.DestroyCompletion, error) {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("destroy completion is not a regular file")
	}
	file, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer file.Close()
	body, err := io.ReadAll(io.LimitReader(file, maxDestroyCompletionBytes+1))
	if err != nil {
		return nil, err
	}
	if len(body) > maxDestroyCompletionBytes {
		return nil, errors.New("destroy completion record exceeds limit")
	}
	var proof nodev1.DestroyCompletion
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&proof); err != nil {
		return nil, err
	}
	if decoder.Decode(new(any)) != io.EOF || !validDestroyCompletion(&proof) {
		return nil, errors.New("invalid destroy completion record")
	}
	// A prior publication may have returned an fsync error after rename. Never
	// promote that visible file to proof without completing its durability barrier.
	if durable {
		if err := file.Sync(); err != nil {
			return nil, err
		}
		if err := syncDestroyDirectory(filepath.Dir(path)); err != nil {
			return nil, err
		}
	}
	return &proof, nil
}

func validDestroyCompletion(p *nodev1.DestroyCompletion) bool {
	return destroyIdentityPattern.MatchString(p.GetOperationId()) &&
		destroyIdentityPattern.MatchString(p.GetVmId()) &&
		destroyIdentityPattern.MatchString(p.GetBootId()) &&
		destroyIdentityPattern.MatchString(p.GetSessionId()) &&
		p.GetNode() != "" && len(p.GetNode()) <= 256 &&
		p.GetPodUid() != "" && len(p.GetPodUid()) <= 256 &&
		p.GetCompletedAtUnixMs() > 0
}

func sameDestroyIdentity(a, b *nodev1.DestroyCompletion) bool {
	return a.GetOperationId() == b.GetOperationId() && a.GetVmId() == b.GetVmId() &&
		a.GetBootId() == b.GetBootId() && a.GetSessionId() == b.GetSessionId() &&
		a.GetNode() == b.GetNode() && a.GetPodUid() == b.GetPodUid()
}

func (p *destroyCompletionStore) lookup(want *nodev1.DestroyCompletion) (*nodev1.DestroyCompletion, error) {
	proof, err := readDestroyCompletion(p.path(want.GetOperationId()), true)
	if err != nil || proof == nil {
		return nil, err
	}
	if !sameDestroyIdentity(proof, want) {
		return nil, status.Error(codes.FailedPrecondition, "noded: conflicting destroy completion identity")
	}
	if p.now().Sub(time.UnixMilli(proof.GetCompletedAtUnixMs())) >= destroyCompletionRetention {
		return nil, nil
	}
	return proof, nil
}

// reserve checks the bounded retained store before beginning destructive work.
// The caller holds the flock through publication. Unknown files/corruption fail
// closed; they never turn a missing proof into affirmative cessation.
func (p *destroyCompletionStore) reserve() error {
	dir, err := os.Open(p.root)
	if err != nil {
		return err
	}
	defer dir.Close()
	entries, err := dir.ReadDir(2*p.limit + 16)
	if err != nil && err != io.EOF {
		return err
	}
	if len(entries) == 2*p.limit+16 {
		return status.Error(codes.ResourceExhausted, "noded: destroy completion inventory exceeds bound")
	}
	retained := 0
	removed := false
	for _, entry := range entries {
		name := entry.Name()
		if name == ".lock" {
			continue
		}
		path := filepath.Join(p.root, name)
		if strings.HasPrefix(name, ".completion-") && !entry.IsDir() {
			// No other writer owns the store lock, so a temporary file is left
			// over from an interrupted write and cannot be a committed proof.
			if err := os.Remove(path); err != nil {
				return err
			}
			removed = true
			continue
		}
		if entry.IsDir() || !strings.HasSuffix(name, ".json") {
			return errors.New("unexpected destroy completion inventory entry")
		}
		proof, err := readDestroyCompletion(path, false)
		if err != nil {
			return fmt.Errorf("read destroy completion inventory: %w", err)
		}
		if proof == nil {
			return errors.New("destroy completion inventory changed during scan")
		}
		if p.now().Sub(time.UnixMilli(proof.GetCompletedAtUnixMs())) >= destroyCompletionRetention {
			if err := os.Remove(path); err != nil {
				return err
			}
			removed = true
		} else {
			retained++
		}
	}
	if removed {
		if err := syncDestroyDirectory(p.root); err != nil {
			return err
		}
	}
	if retained >= p.limit {
		return status.Error(codes.ResourceExhausted, "noded: destroy completion retention full")
	}
	return nil
}

func writeDestroyCompletion(path string, body []byte) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".completion-")
	if err != nil {
		return err
	}
	temp := file.Name()
	defer os.Remove(temp)
	if _, err := file.Write(body); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	if err := os.Rename(temp, path); err != nil {
		return err
	}
	return syncDestroyDirectory(filepath.Dir(path))
}

func (p *destroyCompletionStore) commit(proof *nodev1.DestroyCompletion) error {
	if !validDestroyCompletion(proof) {
		return errors.New("invalid destroy completion identity")
	}
	if prior, err := p.lookup(proof); err != nil {
		return err
	} else if prior != nil {
		delete(p.pending, proof.GetOperationId())
		return nil
	}
	// An earlier failed strict operation may be retried through legacy cleanup
	// after other completions filled the store. It must not exceed the bound.
	if err := p.reserve(); err != nil {
		return err
	}
	body, err := json.Marshal(proof)
	if err != nil {
		return err
	}
	if len(body) > maxDestroyCompletionBytes {
		return errors.New("destroy completion record exceeds limit")
	}
	if err := p.write(p.path(proof.GetOperationId()), body); err != nil {
		return err
	}
	delete(p.pending, proof.GetOperationId())
	return nil
}

func (s *Server) persistDestroyCompletion(proof *nodev1.DestroyCompletion) error {
	unlock, err := s.destroyProofs.lock()
	if err != nil {
		return err
	}
	defer unlock()
	return s.destroyProofs.commit(proof)
}

func (s *Server) destroyStrict(ctx context.Context, req *nodev1.DestroyRequest) (*nodev1.DestroyResponse, error) {
	if !destroyIdentityPattern.MatchString(req.GetOperationId()) ||
		!destroyIdentityPattern.MatchString(req.GetVmId()) ||
		!destroyIdentityPattern.MatchString(req.GetExpectedBootId()) ||
		!destroyIdentityPattern.MatchString(req.GetExpectedSessionId()) {
		return nil, status.Error(codes.InvalidArgument, "noded: complete strict destroy identity required")
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	want := &nodev1.DestroyCompletion{
		OperationId: req.GetOperationId(), VmId: req.GetVmId(),
		BootId: req.GetExpectedBootId(), SessionId: req.GetExpectedSessionId(),
		Node: s.cfg.Node, PodUid: s.cfg.PodUID,
	}
	if want.GetNode() == "" || len(want.GetNode()) > 256 || want.GetPodUid() == "" || len(want.GetPodUid()) > 256 {
		return nil, status.Error(codes.FailedPrecondition, "noded: strict destroy owner unavailable")
	}
	unlock, err := s.destroyProofs.lock()
	if err != nil {
		return nil, destroyProofError(err)
	}
	defer unlock()
	proof, err := s.destroyProofs.lookup(want)
	if err != nil {
		return nil, destroyProofError(err)
	}
	if proof != nil {
		// A previous rename may have succeeded but its durability barrier failed.
		// lookup completed that barrier. Finish only this boot's retained strict
		// entry; a restarted daemon must never reap a new resident on replay.
		if proof.GetBootId() == s.bootID {
			if e := s.sessionVMs.get(proof.GetVmId()); e != nil {
				if e.sessionID != proof.GetSessionId() || e.teardown.completion == nil ||
					!sameDestroyIdentity(e.teardown.completion, proof) {
					return nil, status.Error(codes.FailedPrecondition, "noded: completed destroy conflicts with resident identity")
				}
				if err := s.reapSessionEntryWithProof(e, proof, s.destroyProofs.commit); err != nil {
					return nil, destroyProofError(err)
				}
			}
		}
		return &nodev1.DestroyResponse{TeardownConfirmed: true, Completion: proof}, nil
	}
	if want.GetBootId() != s.bootID {
		return nil, status.Error(codes.FailedPrecondition, "noded: strict destroy boot changed without completion")
	}
	if prior := s.destroyProofs.pending[want.GetOperationId()]; prior != nil && !sameDestroyIdentity(prior, want) {
		return nil, status.Error(codes.FailedPrecondition, "noded: conflicting pending destroy operation")
	}
	// A primed task entry has no authoritative session identity. Do not adopt a
	// caller-supplied session merely to manufacture strict stop evidence.
	s.vmLifecycleMu.Lock()
	e := s.sessionVMs.get(want.GetVmId())
	s.vmLifecycleMu.Unlock()
	if e == nil || e.sessionID != want.GetSessionId() {
		return nil, status.Error(codes.FailedPrecondition, "noded: exact session VM is not owned")
	}
	if err := s.destroyProofs.reserve(); err != nil {
		return nil, destroyProofError(err)
	}
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	e.teardown.started.Store(true)
	if err := s.reapSessionEntryWithProof(e, want, s.destroyProofs.commit); err != nil {
		return nil, destroyProofError(err)
	}
	// Ordinary teardown could have finished between selection and acquiring the
	// retained entry's mutex. Its absence/done flag is not a strict completion.
	proof, err = s.destroyProofs.lookup(want)
	if err != nil {
		return nil, destroyProofError(err)
	}
	if proof == nil {
		return nil, status.Error(codes.Unavailable, "noded: strict destroy completion unavailable")
	}
	delete(s.destroyProofs.pending, want.GetOperationId())
	return &nodev1.DestroyResponse{TeardownConfirmed: true, Completion: proof}, nil
}

func destroyProofError(err error) error {
	if errors.Is(err, errTeardownInProgress) {
		return status.Error(codes.Unavailable, "noded: VM teardown in progress")
	}
	if _, ok := status.FromError(err); ok {
		return err
	}
	return status.Errorf(codes.Unavailable, "noded: destroy completion unavailable: %v", err)
}
