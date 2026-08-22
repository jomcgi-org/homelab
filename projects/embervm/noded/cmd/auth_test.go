package main

import (
	"context"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func incomingBearer(value string) context.Context {
	return metadata.NewIncomingContext(
		context.Background(),
		metadata.Pairs("authorization", value),
	)
}

func TestCheckBearerAcceptsExactToken(t *testing.T) {
	if err := checkBearer(incomingBearer("Bearer node-secret"), "node-secret"); err != nil {
		t.Fatalf("checkBearer rejected exact token: %v", err)
	}
}

func TestCheckBearerRejectsInvalidAuthorization(t *testing.T) {
	tests := []struct {
		name string
		ctx  context.Context
	}{
		{
			name: "wrong token",
			ctx:  incomingBearer("Bearer wrong-secret"),
		},
		{
			name: "missing authorization header",
			ctx:  metadata.NewIncomingContext(context.Background(), metadata.MD{}),
		},
		{
			name: "trailing junk",
			ctx:  incomingBearer("Bearer node-secret trailing-junk"),
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := status.Code(checkBearer(tt.ctx, "node-secret")); got != codes.Unauthenticated {
				t.Fatalf("checkBearer status = %v, want %v", got, codes.Unauthenticated)
			}
		})
	}
}

func TestUnaryAuthInterceptorRejectsBadToken(t *testing.T) {
	called := false
	handler := func(context.Context, any) (any, error) {
		called = true
		return "handled", nil
	}

	got, err := unaryAuthInterceptor("node-secret")(
		incomingBearer("Bearer wrong-secret"),
		nil,
		&grpc.UnaryServerInfo{FullMethod: "/embervm.node.v1.NodeService/Prime"},
		handler,
	)
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("interceptor status = %v, want %v", status.Code(err), codes.Unauthenticated)
	}
	if got != nil {
		t.Fatalf("interceptor response = %#v, want nil", got)
	}
	if called {
		t.Fatal("interceptor called handler for an invalid token")
	}
}

func TestUnaryAuthInterceptorPassesGoodToken(t *testing.T) {
	called := false
	handler := func(context.Context, any) (any, error) {
		called = true
		return "handled", nil
	}

	got, err := unaryAuthInterceptor("node-secret")(
		incomingBearer("Bearer node-secret"),
		nil,
		&grpc.UnaryServerInfo{FullMethod: "/embervm.node.v1.NodeService/Prime"},
		handler,
	)
	if err != nil {
		t.Fatalf("interceptor rejected valid token: %v", err)
	}
	if got != "handled" {
		t.Fatalf("interceptor response = %#v, want handled", got)
	}
	if !called {
		t.Fatal("interceptor did not call handler for a valid token")
	}
}
