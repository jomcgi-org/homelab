package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
)

// iggyTCPPort is the port iggy-server's binary protocol listens on inside the VM
// (the CR's stateful.port, and upstream's own default). noded health-gates a
// stateful runtime boot by TCP-connecting here over the tap; the ready probe in
// iggy_linux.go uses the same port on loopback.
const iggyTCPPort = 8090

// iggyTCPPortString is iggyTCPPort as a string, for the IGGY_TCP_ADDRESS default
// and the dial in waitIggyReady.
var iggyTCPPortString = strconv.Itoa(iggyTCPPort)

// iggyUID / iggyGID are the in-guest account iggy-server runs as, matching the
// apko image's declared `iggy` account. guest-init is PID 1 and root (which it
// must be to mkfs + mount the volume) and drops the server child to this uid.
// Nothing in iggy-server needs privilege: its CPU pinning and NUMA binding are
// unprivileged calls. uid 65532 is the repo-wide non-root image convention.
const (
	iggyUID = 65532
	iggyGID = 65532
)

// defaultRootUsername is upstream's DEFAULT_ROOT_USERNAME. Set explicitly rather
// than left implicit so the credential is fully determined by (this constant, the
// Secret's IGGY_ROOT_PASSWORD) instead of half of it living in an upstream
// default that could move under us on a version bump.
const defaultRootUsername = "iggy"

// rootPasswordEnv is the env var iggy-server reads the root user's password from
// on its first bootstrap. It arrives through the workload's secretRef ->
// mmds_env boot-args seam (D-R4.PR-7.1).
const rootPasswordEnv = "IGGY_ROOT_PASSWORD"

// systemPathEnv is the env var naming iggy-server's data directory root: message
// segments, stream/topic/partition metadata, and the state log all live under it.
const systemPathEnv = "IGGY_SYSTEM_PATH"

// iggyDataDirName is the directory under the volume mount that becomes
// IGGY_SYSTEM_PATH. Nested one level (rather than using the mount root) so the
// volume can hold other guest state without colliding with the server's layout,
// matching how the postgres runtime puts PGDATA at <mount>/pgdata.
const iggyDataDirName = "iggy"

// iggySystemPath returns the data directory for a given volume mount path.
func iggySystemPath(mountPath string) string {
	return filepath.Join(mountPath, iggyDataDirName)
}

// stateLogPath is the server's append-only state log, whose FIRST entry is the
// CreateUser record for the root user. See stateBootstrapped.
func stateLogPath(systemPath string) string {
	return filepath.Join(systemPath, "state", "log")
}

// stateBootstrapped reports whether this volume already carries a bootstrapped
// Iggy system, meaning the root user exists and IGGY_ROOT_PASSWORD is no longer
// needed. A non-empty state log is the marker: iggy-server writes the root user
// as its first state entry, so the file is 0 bytes (or absent) before that and
// non-empty after.
//
// The obvious alternative marker, the presence of <system.path>/state/info, is
// WRONG here: the server writes the system info BEFORE it creates the root user
// ("System info not found, creating..." precedes "No users found, creating the
// root user..."). A first boot interrupted in that window (an aggressive
// idle-bank, or a destroy mid-boot) would leave info present with no root user,
// and an info-keyed probe would then skip the password requirement forever on a
// volume that still needs to bootstrap one. Keying on the state log closes that
// window, the same way the postgres runtime's PG_VERSION probe does.
func stateBootstrapped(systemPath string) (bool, error) {
	info, err := os.Stat(stateLogPath(systemPath))
	if err == nil {
		return info.Size() > 0, nil
	}
	if os.IsNotExist(err) {
		return false, nil
	}
	return false, err
}

// iggyChildEnv builds the environment for the iggy-server child: the inherited
// process env (which already carries setDefaultEnv's IGGY_* defaults and any
// mmds_env overrides) plus IGGY_SYSTEM_PATH pinned to the mounted volume.
//
// IGGY_SYSTEM_PATH is not part of setDefaultEnv because it is only knowable once
// the volume mount path is read off the kernel command line. An operator value
// delivered through mmds_env still wins: it is only defaulted when unset.
func iggyChildEnv(mountPath string) []string {
	env := os.Environ()
	if _, set := os.LookupEnv(systemPathEnv); !set {
		env = append(env, systemPathEnv+"="+iggySystemPath(mountPath))
	}
	return env
}

// requireRootPassword returns an error when this is a first boot (bootstrapped is
// false, from stateBootstrapped) and IGGY_ROOT_PASSWORD is unset.
//
// Without it iggy-server does NOT fail and does NOT use a fixed default: it
// generates a random password and prints it once to stdout (verified against
// 0.8.0). That is worse than either alternative here. The stateful class boots a
// volume from scratch exactly once, so the only copy of the credential would be
// that single boot's log line, unrecoverable as soon as it rotates, on a
// datastore that then outlives it by up to bankedTtlSeconds. Refusing the boot
// turns a silently unusable datastore into a wiring error with a name.
func requireRootPassword(bootstrapped bool) error {
	if bootstrapped {
		return nil
	}
	if os.Getenv(rootPasswordEnv) == "" {
		return fmt.Errorf(
			"%s unset on first boot: iggy-server would autogenerate a root password and print it once, "+
				"which is unrecoverable on a stateful volume (check the CR secretRef and the Secret's %s key)",
			rootPasswordEnv, rootPasswordEnv)
	}
	return nil
}
