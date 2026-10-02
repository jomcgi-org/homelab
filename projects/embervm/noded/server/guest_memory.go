package server

import (
	"context"

	"github.com/jomcgi/homelab/projects/embervm/noded/guestmem"
	"github.com/jomcgi/homelab/projects/embervm/noded/substrate"
)

// StartGuestMemoryFeedback runs a read-only observer. Disabled configurations
// create neither tracking entries nor connections, including on older guests.
func (s *Server) StartGuestMemoryFeedback(ctx context.Context) {
	if s.cfg.GuestMemoryFeedbackEnabled {
		go s.guestMemory.Run(ctx)
	}
}

func (s *Server) trackGuestMemory(h substrate.Handle, workload, uds string) {
	s.guestMemory.Track(guestmem.Target{VM: h.ThreadID, Activation: h.ID, Workload: workload, UDS: uds})
}

func (s *Server) forgetGuestMemory(h substrate.Handle) {
	s.guestMemory.Forget(h.ThreadID, h.ID)
}

func (s *Server) guestMemoryEvent(e guestmem.Event) {
	// Every identity is supplied by the host handle and workload binding.
	s.logger.Info("noded: guest memory observation", "event", e.Kind,
		"vm", e.VM, "workload", e.Workload, "activation", e.Activation, "delta", e.Delta)
	// Do not invoke bank, admission, eviction or pressure-banking from this path.
}
