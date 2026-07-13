package main

import (
	"context"
	"crypto/subtle"
	"strings"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// bearerPrefix is the scheme in the gRPC "authorization" metadata value the
// daemon expects: "Bearer <token>".
const bearerPrefix = "Bearer "

// checkBearer validates the incoming call's authorization metadata against the
// configured static token in constant time. It is the portable, mesh-independent
// gate (v1 auth per the node.proto contract); a Cilium/Linkerd policy layers on
// top. The upgrade path to mTLS/SPIFFE is additive and does not touch this.
func checkBearer(ctx context.Context, token string) error {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return status.Error(codes.Unauthenticated, "noded: missing call metadata")
	}
	vals := md.Get("authorization")
	if len(vals) == 0 {
		return status.Error(codes.Unauthenticated, "noded: missing authorization")
	}
	got := strings.TrimPrefix(vals[0], bearerPrefix)
	if subtle.ConstantTimeCompare([]byte(got), []byte(token)) != 1 {
		return status.Error(codes.Unauthenticated, "noded: invalid bearer token")
	}
	return nil
}

// unaryAuthInterceptor gates every unary RPC on the bearer token.
func unaryAuthInterceptor(token string) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		if err := checkBearer(ctx, token); err != nil {
			return nil, err
		}
		return handler(ctx, req)
	}
}

// streamAuthInterceptor gates every streaming RPC (WatchNode) on the bearer token.
func streamAuthInterceptor(token string) grpc.StreamServerInterceptor {
	return func(srv any, ss grpc.ServerStream, _ *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
		if err := checkBearer(ss.Context(), token); err != nil {
			return err
		}
		return handler(srv, ss)
	}
}
