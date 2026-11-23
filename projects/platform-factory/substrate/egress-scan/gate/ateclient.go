package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/gate/ateapi"
)

// previewHeader opts a call into preview features. CreateActor of a template
// with fork_config needs it (forkForCreate in the fork's controlapi).
const previewHeader = "ate-preview-allowed"

// actor is what the pool needs to know about an ate API actor.
type actor struct {
	Name    string
	Running bool
	// Started is when the actor became RUNNING, if the API says so.
	Started time.Time
	// Created is the creation time, if the API says so.
	Created time.Time
}

var errNotFound = errors.New("actor not found")

// errUnavailable means that the API could not serve the call: it is down, has
// no capacity, or answered too late. The pool backs off on it.
var errUnavailable = errors.New("ate API unavailable")

// actorAPI is the part of the ate API the gate uses. The atespace and the
// template are fixed when the client is built, so no method takes either from
// a caller.
type actorAPI interface {
	// Create makes a waiting fork actor of the template, named name.
	Create(ctx context.Context, name string) (actor, error)
	// Resume forks the child. It returns when the child is RUNNING.
	Resume(ctx context.Context, name string) (actor, error)
	Get(ctx context.Context, name string) (actor, error)
	// Delete removes an actor. A missing actor is errNotFound.
	Delete(ctx context.Context, name string) error
	// List returns the actors whose names start with prefix.
	List(ctx context.Context, prefix string) ([]actor, error)
}

type grpcAPI struct {
	client   ateapi.ControlClient
	atespace string
	template string
}

func newGRPCAPI(conn grpc.ClientConnInterface, atespace, template string) *grpcAPI {
	return &grpcAPI{client: ateapi.NewControlClient(conn), atespace: atespace, template: template}
}

func (a *grpcAPI) ref(name string) *ateapi.ObjectRef {
	return &ateapi.ObjectRef{Atespace: a.atespace, Name: name}
}

func (a *grpcAPI) Create(ctx context.Context, name string) (actor, error) {
	got, err := a.client.CreateActor(ctx, &ateapi.CreateActorRequest{Actor: &ateapi.Actor{
		Metadata:      &ateapi.ResourceMetadata{Atespace: a.atespace, Name: name},
		ActorTemplate: &ateapi.ObjectRef{Atespace: a.atespace, Name: a.template},
	}})
	return fromProto(got), mapErr(err)
}

func (a *grpcAPI) Resume(ctx context.Context, name string) (actor, error) {
	got, err := a.client.ResumeActor(ctx, &ateapi.ResumeActorRequest{Actor: a.ref(name)})
	return fromProto(got.GetActor()), mapErr(err)
}

func (a *grpcAPI) Get(ctx context.Context, name string) (actor, error) {
	got, err := a.client.GetActor(ctx, &ateapi.GetActorRequest{Actor: a.ref(name)})
	return fromProto(got), mapErr(err)
}

func (a *grpcAPI) Delete(ctx context.Context, name string) error {
	_, err := a.client.DeleteActor(ctx, &ateapi.DeleteActorRequest{Actor: a.ref(name)})
	return mapErr(err)
}

func (a *grpcAPI) List(ctx context.Context, prefix string) ([]actor, error) {
	var out []actor
	token := ""
	for page := 0; page < 100; page++ {
		resp, err := a.client.ListActors(ctx, &ateapi.ListActorsRequest{
			Atespace: a.atespace,
			Options:  &ateapi.ListOptions{PageSize: 200, PageToken: token},
		})
		if err != nil {
			return nil, mapErr(err)
		}
		for _, item := range resp.GetActors() {
			// Seeds are system actors of the same atespace. Only names with
			// this pod's prefix are ours.
			if strings.HasPrefix(item.GetMetadata().GetName(), prefix) {
				out = append(out, fromProto(item))
			}
		}
		if token = resp.GetNextPageToken(); token == "" {
			return out, nil
		}
	}
	return nil, errors.New("actor list has too many pages")
}

func fromProto(a *ateapi.Actor) actor {
	out := actor{
		Name:    a.GetMetadata().GetName(),
		Running: a.GetStatus().GetState() == ateapi.ActorState_ACTOR_STATE_RUNNING,
	}
	if ts := a.GetMetadata().GetCreateTime(); ts != nil {
		out.Created = ts.AsTime()
	}
	if ts := a.GetStatus().GetFork().GetStartTime(); ts != nil {
		out.Started = ts.AsTime()
	}
	return out
}

// mapErr keeps the gRPC code and drops the message: a message can quote the
// request.
func mapErr(err error) error {
	if err == nil {
		return nil
	}
	switch status.Code(err) {
	case codes.NotFound:
		return errNotFound
	case codes.Unavailable, codes.DeadlineExceeded, codes.ResourceExhausted, codes.Canceled, codes.Aborted:
		return fmt.Errorf("%w: %s", errUnavailable, status.Code(err))
	}
	return fmt.Errorf("ate API: %s", status.Code(err))
}

// bearerFile sends the projected ServiceAccount token as a bearer token. It
// reads the file on each call, so a rotated token takes effect at once.
type bearerFile struct {
	path       string
	requireTLS bool
}

func (b bearerFile) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	data, err := os.ReadFile(b.path)
	if err != nil {
		return nil, fmt.Errorf("read token file: %w", err)
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return nil, errors.New("token file is empty")
	}
	return map[string]string{"authorization": "Bearer " + token}, nil
}

func (b bearerFile) RequireTransportSecurity() bool { return b.requireTLS }

// roundRobin spreads calls over every ate API replica: the Service is headless.
const roundRobin = `{"loadBalancingConfig": [{"round_robin":{}}]}`

func dialAPI(cfg config, tls *tlsSource) (*grpc.ClientConn, error) {
	return grpc.NewClient(cfg.APIEndpoint,
		grpc.WithTransportCredentials(credentials.NewTLS(tls.config(cfg.APIServerName, false))),
		grpc.WithPerRPCCredentials(bearerFile{path: cfg.APITokenFile, requireTLS: true}),
		grpc.WithDefaultServiceConfig(roundRobin),
		grpc.WithChainUnaryInterceptor(previewInterceptor),
	)
}

func previewInterceptor(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {
	return invoker(metadata.AppendToOutgoingContext(ctx, previewHeader, "true"), method, req, reply, cc, opts...)
}
