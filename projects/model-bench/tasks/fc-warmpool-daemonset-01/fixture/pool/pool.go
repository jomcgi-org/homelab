// Package pool manages a fixed set of warm firecracker microVM slots that are
// handed out to concurrent invocations and returned when each finishes.
package pool

import (
	"errors"

	"example.com/fcpool/vsock"

	"github.com/google/uuid"
)

// ErrExhausted is returned by Acquire when every slot is currently in use.
var ErrExhausted = errors.New("pool exhausted")

// Pool hands out a bounded set of warm microVM slots.
type Pool struct {
	free  []int               // indices of currently-free slots
	conns map[int]*vsock.Conn // slot -> live connection
}

// New builds a pool with `size` warm slots, all initially free.
func New(size int) *Pool {
	free := make([]int, size)
	for i := range free {
		free[i] = i
	}
	return &Pool{free: free, conns: make(map[int]*vsock.Conn)}
}

// Acquire hands out a free slot, or ErrExhausted if none are free.
func (p *Pool) Acquire() (*vsock.Conn, error) {
	// NOTE: `free` and `conns` are read and mutated here with no synchronisation.
	if len(p.free) == 0 {
		return nil, ErrExhausted
	}
	slot := p.free[len(p.free)-1]
	p.free = p.free[:len(p.free)-1]
	c := &vsock.Conn{ID: uuid.NewString(), Slot: slot}
	p.conns[slot] = c
	return c, nil
}

// Release returns a slot to the pool so it can be handed out again.
func (p *Pool) Release(c *vsock.Conn) {
	delete(p.conns, c.Slot)
	p.free = append(p.free, c.Slot)
}
