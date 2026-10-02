//go:build !linux

package driver

import "errors"

// Non-Linux callers can still Kill and Wait with the existing contract, but
// cannot claim host_requested without the non-reaping Linux liveness evidence.
func processRunning(_ int) (bool, error) {
	return false, errors.New("non-reaping process liveness observation requires Linux")
}
