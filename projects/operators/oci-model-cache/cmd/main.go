/*
Copyright 2025.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package main

import (
	"context"
	"crypto/tls"
	"flag"
	"os"
	"path/filepath"
	"time"

	_ "k8s.io/client-go/plugin/pkg/client/auth"

	"k8s.io/apimachinery/pkg/runtime"
	utilruntime "k8s.io/apimachinery/pkg/util/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/certwatcher"
	"sigs.k8s.io/controller-runtime/pkg/healthz"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"
	"sigs.k8s.io/controller-runtime/pkg/metrics/filters"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/copy"
	"github.com/jomcgi/homelab/bazel/tools/hf2oci/pkg/hf"
	v1alpha1 "github.com/jomcgi/homelab/projects/operators/oci-model-cache/api/v1alpha1"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/config"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/controller"
	sm "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/statemachine"
	"github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/telemetry"
	webhookpkg "github.com/jomcgi/homelab/projects/operators/oci-model-cache/internal/webhook"
)

var (
	scheme   = runtime.NewScheme()
	setupLog = ctrl.Log.WithName("setup")
)

func init() {
	utilruntime.Must(clientgoscheme.AddToScheme(scheme))
	utilruntime.Must(v1alpha1.AddToScheme(scheme))
}

// hf2ociResolver adapts hf2oci's copy.Resolve to the controller's Resolver interface.
// This is the ONLY file that imports bazel/tools/hf2oci.
type hf2ociResolver struct {
	client *hf.Client
}

func (r *hf2ociResolver) Resolve(ctx context.Context, repo, registry, revision, file string) (*controller.ResolveResult, error) {
	result, err := copy.Resolve(ctx, copy.ResolveOptions{
		Repo:     repo,
		Registry: registry,
		Revision: revision,
		File:     file,
		HFClient: r.client,
	})
	if err != nil {
		// Adapt hf2oci PermanentError to controller PermanentError
		if copy.IsPermanent(err) {
			return nil, &controller.PermanentError{Err: err}
		}
		return nil, err
	}

	return &controller.ResolveResult{
		Ref:       result.Ref,
		Digest:    result.Digest,
		Cached:    result.Cached,
		Revision:  result.Revision,
		Format:    string(result.Format),
		FileCount: result.FileCount,
		TotalSize: result.TotalSize,
	}, nil
}

// nolint:gocyclo
func main() {
	var metricsAddr string
	var metricsCertPath, metricsCertName, metricsCertKey string
	var webhookCertPath, webhookCertName, webhookCertKey string
	var enableLeaderElection bool
	var probeAddr string
	var secureMetrics bool
	var enableHTTP2 bool
	var tlsOpts []func(*tls.Config)

	flag.StringVar(&metricsAddr, "metrics-bind-address", "0",
		"The address the metrics endpoint binds to. Use :8443 for HTTPS or :8080 for HTTP.")
	flag.StringVar(&probeAddr, "health-probe-bind-address", ":8081",
		"The address the probe endpoint binds to.")
	flag.BoolVar(&enableLeaderElection, "leader-elect", false,
		"Enable leader election for controller manager.")
	flag.BoolVar(&secureMetrics, "metrics-secure", true,
		"If set, the metrics endpoint is served securely via HTTPS.")
	flag.StringVar(&webhookCertPath, "webhook-cert-path", "",
		"The directory that contains the webhook certificate.")
	flag.StringVar(&webhookCertName, "webhook-cert-name", "tls.crt",
		"The name of the webhook certificate file.")
	flag.StringVar(&webhookCertKey, "webhook-cert-key", "tls.key",
		"The name of the webhook key file.")
	flag.StringVar(&metricsCertPath, "metrics-cert-path", "",
		"The directory that contains the metrics server certificate.")
	flag.StringVar(&metricsCertName, "metrics-cert-name", "tls.crt",
		"The name of the metrics server certificate file.")
	flag.StringVar(&metricsCertKey, "metrics-cert-key", "tls.key",
		"The name of the metrics server key file.")
	flag.BoolVar(&enableHTTP2, "enable-http2", false,
		"If set, HTTP/2 will be enabled for the metrics and webhook servers.")

	var cfg config.Config
	cfg.BindFlags(flag.CommandLine)

	opts := zap.Options{Development: true}
	opts.BindFlags(flag.CommandLine)
	flag.Parse()

	ctrl.SetLogger(zap.New(zap.UseFlagOptions(&opts)))

	// Initialize OpenTelemetry tracing
	tp, err := telemetry.InitializeTracing(context.Background())
	if err != nil {
		setupLog.Error(err, "failed to initialize OpenTelemetry tracing")
		os.Exit(1)
	}

	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" {
		setupLog.Info("OpenTelemetry tracing enabled",
			"endpoint", os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
			"serviceName", os.Getenv("OTEL_SERVICE_NAME"),
		)
	} else {
		setupLog.V(1).Info("OpenTelemetry tracing disabled (no OTEL_EXPORTER_OTLP_ENDPOINT set)")
	}

	defer func() {
		if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") != "" {
			setupLog.Info("shutting down OpenTelemetry tracer provider")
			if err := telemetry.Shutdown(context.Background(), tp); err != nil {
				setupLog.Error(err, "failed to shutdown tracer provider")
			}
		}
	}()

	// Disable HTTP/2 by default for security (CVE mitigations)
	disableHTTP2 := func(c *tls.Config) {
		setupLog.Info("disabling http/2")
		c.NextProtos = []string{"http/1.1"}
	}
	if !enableHTTP2 {
		tlsOpts = append(tlsOpts, disableHTTP2)
	}

	// Certificate watchers
	var metricsCertWatcher, webhookCertWatcher *certwatcher.CertWatcher

	webhookTLSOpts := tlsOpts
	if len(webhookCertPath) > 0 {
		setupLog.Info("Initializing webhook certificate watcher",
			"webhook-cert-path", webhookCertPath)

		var err error
		webhookCertWatcher, err = certwatcher.New(
			filepath.Join(webhookCertPath, webhookCertName),
			filepath.Join(webhookCertPath, webhookCertKey),
		)
		if err != nil {
			setupLog.Error(err, "Failed to initialize webhook certificate watcher")
			os.Exit(1)
		}

		webhookTLSOpts = append(webhookTLSOpts, func(config *tls.Config) {
			config.GetCertificate = webhookCertWatcher.GetCertificate
		})
	}

	webhookServer := webhook.NewServer(webhook.Options{
		TLSOpts: webhookTLSOpts,
	})

	metricsServerOptions := metricsserver.Options{
		BindAddress:   metricsAddr,
		SecureServing: secureMetrics,
		TLSOpts:       tlsOpts,
	}

	if secureMetrics {
		metricsServerOptions.FilterProvider = filters.WithAuthenticationAndAuthorization
	}

	if len(metricsCertPath) > 0 {
		setupLog.Info("Initializing metrics certificate watcher",
			"metrics-cert-path", metricsCertPath)

		var err error
		metricsCertWatcher, err = certwatcher.New(
			filepath.Join(metricsCertPath, metricsCertName),
			filepath.Join(metricsCertPath, metricsCertKey),
		)
		if err != nil {
			setupLog.Error(err, "Failed to initialize metrics certificate watcher")
			os.Exit(1)
		}

		metricsServerOptions.TLSOpts = append(metricsServerOptions.TLSOpts, func(config *tls.Config) {
			config.GetCertificate = metricsCertWatcher.GetCertificate
		})
	}

	mgr, err := ctrl.NewManager(ctrl.GetConfigOrDie(), ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsServerOptions,
		WebhookServer:          webhookServer,
		HealthProbeBindAddress: probeAddr,
		LeaderElection:         enableLeaderElection,
		LeaderElectionID:       "oci-model-cache.jomcgi.dev",
	})
	if err != nil {
		setupLog.Error(err, "unable to start manager")
		os.Exit(1)
	}

	// Initialize HuggingFace client with 1-hour response cache — Tree and ModelInfo
	// responses are effectively immutable for a given repo+revision.
	hfOpts := []hf.Option{hf.WithCacheTTL(1 * time.Hour)}
	if token := os.Getenv("HF_TOKEN"); token != "" {
		hfOpts = append(hfOpts, hf.WithToken(token))
		setupLog.Info("HuggingFace token configured")
	}
	hfClient := hf.NewClient(hfOpts...)

	// Initialize state machine calculator and observer
	calculator := sm.NewModelCacheCalculator(ctrl.Log.WithName("calculator"))
	observer := sm.CompositeObserver{
		sm.LoggingObserver{},
		sm.NewOTelObserver("oci-model-cache-controller"),
		sm.NewMetricsObserver(),
	}

	// Create and register the reconciler
	reconciler := &controller.ModelCacheReconciler{
		Client:     mgr.GetClient(),
		Scheme:     mgr.GetScheme(),
		Resolver:   &hf2ociResolver{client: hfClient},
		Calculator: calculator,
		Observer:   observer,
		Config:     cfg,
	}

	if err := reconciler.SetupWithManager(mgr); err != nil {
		setupLog.Error(err, "unable to create controller", "controller", "ModelCache")
		os.Exit(1)
	}

	// Register webhook
	if os.Getenv("ENABLE_WEBHOOKS") != "false" {
		setupLog.Info("Registering pod mutating webhook")
		mgr.GetWebhookServer().Register("/mutate-v1-pod", &webhook.Admission{
			Handler: &webhookpkg.PodMutator{
				Client:   mgr.GetClient(),
				Decoder:  admission.NewDecoder(mgr.GetScheme()),
				Registry: cfg.Registry,
				HFClient: hfClient,
			},
		})
	}

	// Register TTL sweeper
	if err := mgr.Add(&controller.TTLSweeper{
		Client:   mgr.GetClient(),
		Interval: 10 * time.Minute,
	}); err != nil {
		setupLog.Error(err, "unable to add TTL sweeper")
		os.Exit(1)
	}

	// Certificate watchers
	if metricsCertWatcher != nil {
		if err := mgr.Add(metricsCertWatcher); err != nil {
			setupLog.Error(err, "unable to add metrics certificate watcher")
			os.Exit(1)
		}
	}
	if webhookCertWatcher != nil {
		if err := mgr.Add(webhookCertWatcher); err != nil {
			setupLog.Error(err, "unable to add webhook certificate watcher")
			os.Exit(1)
		}
	}

	if err := mgr.AddHealthzCheck("healthz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up health check")
		os.Exit(1)
	}
	if err := mgr.AddReadyzCheck("readyz", healthz.Ping); err != nil {
		setupLog.Error(err, "unable to set up ready check")
		os.Exit(1)
	}

	setupLog.Info("starting manager")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		setupLog.Error(err, "problem running manager")
		os.Exit(1)
	}
}
