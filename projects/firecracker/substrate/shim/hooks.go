// Package shim is the guest-side HTTP shim for the fc-invoke substrate (ADR 029).
// It dispatches /invoke requests to a workload Handler, exposes a /shim/*
// control surface, and runs an optional pre/post hook Chain around each
// invocation. The server accepts any net.Listener (vsock in production,
// TCP/UDS in tests).
package shim

import (
	"context"
	"errors"
)

// PreHook runs before a Handler is called. If it returns an error the
// invocation is aborted and the Handler is not called.
type PreHook func(ctx context.Context, r *Request) error

// PostHook runs after a Handler returns, regardless of the handler result.
// Post-hooks are best-effort: all of them run even when an earlier one errors.
type PostHook func(ctx context.Context, r *Request, resp *Response) error

// Chain is an ordered sequence of pre- and post-invocation hooks.
// Pre-hooks gate the invocation; post-hooks observe and persist the result.
type Chain struct {
	Pre  []PreHook
	Post []PostHook
}

// Run executes the hook chain around h:
//  1. Each PreHook runs in order; the first error aborts immediately and h is
//     not called.
//  2. h is called (skipped on any PreHook failure).
//  3. Every PostHook runs in order regardless of individual errors; all post
//     errors are collected via errors.Join.
//
// If h itself returns an error, that error (joined with any post errors) is
// returned alongside h's (possibly nil) Response.
func (c Chain) Run(ctx context.Context, r *Request, h Handler) (*Response, error) {
	for _, pre := range c.Pre {
		if err := pre(ctx, r); err != nil {
			return nil, err
		}
	}

	resp, handlerErr := h(ctx, r)

	var postErr error
	for _, post := range c.Post {
		if err := post(ctx, r, resp); err != nil {
			postErr = errors.Join(postErr, err)
		}
	}

	return resp, errors.Join(handlerErr, postErr)
}
