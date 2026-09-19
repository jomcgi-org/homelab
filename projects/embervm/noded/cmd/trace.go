package main

import (
	"context"

	"go.opentelemetry.io/otel/propagation"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
)

// incomingMetadataCarrier adapts gRPC metadata to OpenTelemetry's standard
// text-map carrier without changing the metadata passed to the RPC handler.
type incomingMetadataCarrier metadata.MD

func (c incomingMetadataCarrier) Get(key string) string {
	values := metadata.MD(c).Get(key)
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func (c incomingMetadataCarrier) Set(key, value string) {
	metadata.MD(c).Set(key, value)
}

func (c incomingMetadataCarrier) Keys() []string {
	md := metadata.MD(c)
	keys := make([]string, 0, len(md))
	for key := range md {
		keys = append(keys, key)
	}
	return keys
}

// unaryTraceContextInterceptor extracts W3C traceparent metadata into the RPC
// context. TraceContext rejects malformed input and leaves the original context
// intact, including its deadline and cancellation chain.
func unaryTraceContextInterceptor() grpc.UnaryServerInterceptor {
	propagator := propagation.TraceContext{}
	return func(ctx context.Context, req any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		if md, ok := metadata.FromIncomingContext(ctx); ok {
			ctx = propagator.Extract(ctx, incomingMetadataCarrier(md))
		}
		return handler(ctx, req)
	}
}

// grpcServerOptions is the one production server-option path. Keeping tracing
// here makes the interceptor active whether bearer authentication is enabled or
// disabled, while preserving the existing auth order and stream behavior.
func grpcServerOptions(bearerToken string) []grpc.ServerOption {
	unary := []grpc.UnaryServerInterceptor{unaryTraceContextInterceptor()}
	if bearerToken != "" {
		unary = append([]grpc.UnaryServerInterceptor{unaryAuthInterceptor(bearerToken)}, unary...)
	}

	options := []grpc.ServerOption{grpc.ChainUnaryInterceptor(unary...)}
	if bearerToken != "" {
		options = append(options, grpc.StreamInterceptor(streamAuthInterceptor(bearerToken)))
	}
	return options
}
