// Package detect is the plug-in point of the egress scanner for credential
// detectors. The scanner host (package main) calls exactly one detector for each
// request. This package holds the contract, the registry and the host wrapper
// that enforces limits, so that a detector can be replaced or joined by another
// one without a change to the host.
//
// # Contract
//
// A Detector scans one Input and returns the findings it has. It must:
//
//   - stop when ctx is done, and return ctx's error or ErrBudget. The host also
//     stops waiting at its deadline, but a detector that ignores ctx keeps a CPU;
//   - never return, log or wrap the matched text. A Finding names a rule and
//     offsets, nothing else. The host never logs request bytes;
//   - never skip a finding because the input says so (an "allow" marker in the
//     text): the input is untrusted;
//   - be safe for concurrent calls;
//   - return a Finding of ClassBlock for a credential. A ClassReport finding is
//     counted and never denies a request (for a rule that is only an indicator).
//
// A detector may stop at its first ClassBlock finding. The host denies on any.
//
// The host enforces what a detector cannot be trusted to enforce: the input
// size limit (Limits.MaxInputBytes), the deadline (Limits.Deadline), a panic
// (the call fails), the mode (observe never denies) and the failure mode (what
// a call without a verdict does: deny or allow). These come from configuration
// (Config), per detector, never from the detector's own code.
//
// # Private data
//
// A detector that holds private data (for example an index of known private
// content) implements PrivateData. The host calls Load with Config.DataPath at
// start, and Reload and Health on request. Betterleaks and Gitleaks do not.
//
// # How to swap or add a detector
//
//  1. Write a package that implements Detector and calls Register in init with
//     a unique name and a Factory.
//  2. Import the package for its side effect in the binary: a blank import in
//     main (public detectors, detectors.go) or in a file of a private build
//     (detectors_private.go, never committed to the public repository).
//  3. Run detecttest.Conformance on the Factory with a fixture of your own. It
//     is the test every detector must pass.
//  4. Select it with the --detector flag (values: detector.name). The mode, the
//     failure mode, the limits and the data path are flags next to it.
//
// The host calls one detector. Joining two detectors is a change to the host
// (a list of Hosts and a rule for the verdicts), not to this package.
package detect
