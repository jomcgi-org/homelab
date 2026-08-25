package server

// Register-time snapshot data-format validation (#4407).
//
// A Firecracker snapshot embeds a DATA FORMAT VERSION (independent of the
// Firecracker release version). On restore, Firecracker refuses any snapshot
// whose format major differs from its own, or whose minor is newer
// (`Snapshot::load_without_crc_check`: same major, file minor <= binary minor),
// so a base written by a different Firecracker generation than the one a node
// runs today can never be loaded there: every dispatch resting on that base
// aborts with a hard PUT /snapshot/load failure. Historically the only defense
// was the manual hypervisorEpoch ride-along (a chart value humans must bump in
// the same PR as the binary); forgetting it meant fleet-wide dispatch failures
// discovered post-merge.
//
// This file replaces that manual contract with ground truth. The binary noded
// ships knows the format it supports (`firecracker --snapshot-version`), and a
// snapshot file states the format it was written in (`firecracker --describe-
// snapshot <path>`), so the boot reconcile (and every later disk adoption) can
// refuse to register an incompatible base BEFORE anything rests on it. A
// refused base is reported BASE_BUILD_STATE_NONE with the reason in buildErr,
// exactly like the missing-rootfs branch: NONE is the only state the control
// plane acts on, so refusal lands in the normal absent-base rebuild machinery
// instead of a per-dispatch abort.
//
// Fail-open rule: if we cannot learn what OUR OWN binary supports (the probe
// itself fails, e.g. BinPath unset in tests), we skip validation entirely and
// keep the pre-existing behavior. Absent information never invalidates working
// state (the same convention as the rootfsPath handling). But a snapfile the
// binary cannot DESCRIBE while the binary itself probes fine is refused: load
// runs the same parse describe does plus stricter checks, so undescribable
// means unloadable.

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// fcProbeTimeout bounds one firecracker --snapshot-version / --describe-snapshot
// invocation. Both exit immediately (they print and leave, no VM), so seconds
// are generous; the bound exists so a wedged binary can never stall daemon boot,
// which blocks on ReconcileBasesFromDisk before serving.
const fcProbeTimeout = 5 * time.Second

// snapVersion is a parsed Firecracker snapshot data-format version
// (MAJOR.MINOR.PATCH, printed by firecracker with a leading "v").
type snapVersion struct {
	Major int
	Minor int
	Patch int
}

func (v snapVersion) String() string {
	return fmt.Sprintf("v%d.%d.%d", v.Major, v.Minor, v.Patch)
}

// parseSnapVersion extracts the first line of `out` that is exactly a
// MAJOR.MINOR.PATCH triple with an optional leading "v", returning ok=false
// when none matches. Whole-line matching (not token scanning) keeps Firecracker's
// stdout log noise, which shares the stream with the version line, from ever
// parsing as a version: log lines lead with a timestamp, so they never match.
func parseSnapVersion(out string) (snapVersion, bool) {
	for _, line := range strings.Split(out, "\n") {
		tok := strings.TrimSpace(line)
		tok = strings.TrimPrefix(tok, "v")
		parts := strings.Split(tok, ".")
		if len(parts) != 3 {
			continue
		}
		var nums [3]int
		valid := true
		for i, p := range parts {
			n, err := strconv.Atoi(p)
			if err != nil || n < 0 {
				valid = false
				break
			}
			nums[i] = n
		}
		if valid {
			return snapVersion{Major: nums[0], Minor: nums[1], Patch: nums[2]}, true
		}
	}
	return snapVersion{}, false
}

// snapshotFormatCompatible mirrors Firecracker's own load gate exactly
// (`Snapshot::load_without_crc_check`): the file loads only when its format
// major equals the binary's and its minor is not newer. Any patch combination
// is accepted, mirroring upstream.
func snapshotFormatCompatible(file, bin snapVersion) bool {
	return file.Major == bin.Major && file.Minor <= bin.Minor
}

// fcSnapshotSupportedVersion asks the binary which snapshot data format it
// supports (`firecracker --snapshot-version`, e.g. "v10.0.0" for v1.16.1).
func fcSnapshotSupportedVersion(binPath string) (snapVersion, error) {
	ctx, cancel := context.WithTimeout(context.Background(), fcProbeTimeout)
	defer cancel()

	out, err := exec.CommandContext(ctx, binPath, "--snapshot-version").Output()
	if err != nil {
		return snapVersion{}, fmt.Errorf("firecracker --snapshot-version: %w", err)
	}

	v, ok := parseSnapVersion(string(out))
	if !ok {
		return snapVersion{}, fmt.Errorf("firecracker --snapshot-version printed no version in %q", strings.TrimSpace(string(out)))
	}
	return v, nil
}

// fcDescribeSnapshotVersion asks the binary to read the data format recorded in
// a snapshot STATE FILE (`firecracker --describe-snapshot <path>`). A non-zero
// exit (unreadable file, foreign format the current bitcode structs cannot
// parse) is an error: restore would fail the identical parse, so the caller
// treats every error as "this base cannot load here".
func fcDescribeSnapshotVersion(binPath, snapfile string) (snapVersion, error) {
	ctx, cancel := context.WithTimeout(context.Background(), fcProbeTimeout)
	defer cancel()

	out, err := exec.CommandContext(ctx, binPath, "--describe-snapshot", snapfile).Output()
	if err != nil {
		return snapVersion{}, fmt.Errorf("firecracker --describe-snapshot: %w", err)
	}

	v, ok := parseSnapVersion(string(out))
	if !ok {
		return snapVersion{}, errors.New("firecracker --describe-snapshot printed no version")
	}
	return v, nil
}

// supportedSnapshotFormat returns the running binary's snapshot data-format
// version, or nil when unknown (the probe has never succeeded). A success is
// cached forever (the binary is baked into the image and cannot change under a
// running pod); a failure is retried on the next call so a transient exec
// problem cannot disable validation for the process lifetime.
func (s *Server) supportedSnapshotFormat() *snapVersion {
	s.fcVerMu.Lock()
	defer s.fcVerMu.Unlock()
	if s.fcSupportedVer != nil {
		return s.fcSupportedVer
	}
	v, err := s.fcSupportedVersionFn(s.cfg.BinPath)
	if err != nil {
		s.logger.Debug("noded: snapshot format probe failed, skipping register-time validation",
			"bin", s.cfg.BinPath, "err", err)
		return nil
	}
	s.fcSupportedVer = &v
	return &v
}

// snapshotFormatRefusal returns "" when snapfile is usable by the local
// binary (or when validation must fail open), otherwise a human-readable
// refusal reason destined for the base entry's buildErr.
func (s *Server) snapshotFormatRefusal(snapfile string) string {
	bin := s.supportedSnapshotFormat()
	if bin == nil {
		// Fail open: without knowing our own binary's format support we cannot
		// judge the file, and absent information never invalidates a base.
		return ""
	}
	file, err := s.fcDescribeVersionFn(s.cfg.BinPath, snapfile)
	if err != nil {
		return fmt.Sprintf("snapshot state file unreadable by this node's firecracker (%v)", err)
	}
	if !snapshotFormatCompatible(file, *bin) {
		return fmt.Sprintf(
			"snapshot data format %s incompatible with this node's firecracker (supports %s)",
			file, bin,
		)
	}
	return ""
}
