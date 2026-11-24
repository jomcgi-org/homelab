package main

// The public detectors. A private build adds its own detector with a file of the
// same shape (detectors_private.go, not committed to this repository) that
// imports the private package for its init, and the host does not change. See
// the package documentation of detect.
import (
	_ "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect/betterleaks"
	_ "github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/detect/gitleaks"
)
