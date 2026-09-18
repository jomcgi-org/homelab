package shim

import (
	"context"
	"errors"
	"testing"
)

// TestChainRunsPreHandlerPost verifies that pre-hooks, the handler, and
// post-hooks execute in that strict order.
func TestChainRunsPreHandlerPost(t *testing.T) {
	var order []string

	c := Chain{
		Pre: []PreHook{
			func(_ context.Context, _ *Request) error {
				order = append(order, "pre")
				return nil
			},
		},
		Post: []PostHook{
			func(_ context.Context, _ *Request, _ *Response) error {
				order = append(order, "post")
				return nil
			},
		},
	}

	h := func(_ context.Context, _ *Request) (*Response, error) {
		order = append(order, "handler")
		return &Response{Status: 200}, nil
	}

	_, err := c.Run(context.Background(), &Request{}, h)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	want := []string{"pre", "handler", "post"}
	if len(order) != len(want) {
		t.Fatalf("execution order %v, want %v", order, want)
	}
	for i, s := range want {
		if order[i] != s {
			t.Errorf("order[%d] = %q, want %q", i, order[i], s)
		}
	}
}

// TestChainPreHookErrorSkipsHandler verifies that a failing pre-hook causes
// the chain to abort immediately, returning the pre-hook error without ever
// calling the handler.
func TestChainPreHookErrorSkipsHandler(t *testing.T) {
	errPre := errors.New("pre failed")
	handlerCalled := false

	c := Chain{
		Pre: []PreHook{
			func(_ context.Context, _ *Request) error { return errPre },
		},
	}

	h := func(_ context.Context, _ *Request) (*Response, error) {
		handlerCalled = true
		return &Response{Status: 200}, nil
	}

	resp, err := c.Run(context.Background(), &Request{}, h)
	if handlerCalled {
		t.Error("handler was called despite pre-hook failure")
	}
	if resp != nil {
		t.Errorf("expected nil response on pre-hook failure, got %+v", resp)
	}
	if !errors.Is(err, errPre) {
		t.Errorf("got error %v, want %v", err, errPre)
	}
}

// TestChainPostHooksAllRunAndJoin verifies that all post-hooks run even when
// an earlier one returns an error, and that all post errors are joined into
// the returned error so errors.Is works for each.
func TestChainPostHooksAllRunAndJoin(t *testing.T) {
	errA := errors.New("post error A")
	errB := errors.New("post error B")
	var postsCalled []int

	c := Chain{
		Post: []PostHook{
			func(_ context.Context, _ *Request, _ *Response) error {
				postsCalled = append(postsCalled, 1)
				return errA
			},
			func(_ context.Context, _ *Request, _ *Response) error {
				postsCalled = append(postsCalled, 2)
				return errB
			},
		},
	}

	h := func(_ context.Context, _ *Request) (*Response, error) {
		return &Response{Status: 200}, nil
	}

	_, err := c.Run(context.Background(), &Request{}, h)

	if len(postsCalled) != 2 {
		t.Errorf("expected both post-hooks to run, called indices: %v", postsCalled)
	}
	if !errors.Is(err, errA) {
		t.Errorf("expected returned error to wrap errA: %v", err)
	}
	if !errors.Is(err, errB) {
		t.Errorf("expected returned error to wrap errB: %v", err)
	}
}
