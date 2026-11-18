package main

import (
	"errors"
	"net/http"
	"os"

	"github.com/spf13/cobra"
	"k8s.io/cli-runtime/pkg/genericclioptions"
	"k8s.io/cli-runtime/pkg/genericiooptions"
	"k8s.io/client-go/rest"
	"k8s.io/kubectl/pkg/cmd/apiresources"
	"k8s.io/kubectl/pkg/cmd/auth"
	"k8s.io/kubectl/pkg/cmd/describe"
	"k8s.io/kubectl/pkg/cmd/events"
	"k8s.io/kubectl/pkg/cmd/explain"
	"k8s.io/kubectl/pkg/cmd/get"
	"k8s.io/kubectl/pkg/cmd/top"
	cmdutil "k8s.io/kubectl/pkg/cmd/util"
	"k8s.io/kubectl/pkg/cmd/version"
)

var browserTLSError = errors.New("custom TLS settings are unsupported in browser kubectl; use a browser-trusted endpoint")

func configureBrowserTLS(root *cobra.Command, flags *genericclioptions.ConfigFlags) {
	tlsFlags := []string{"certificate-authority", "client-certificate", "client-key", "insecure-skip-tls-verify", "tls-server-name"}
	for _, name := range tlsFlags {
		root.PersistentFlags().Lookup(name).Hidden = true
	}
	root.PersistentPreRunE = func(cmd *cobra.Command, _ []string) error {
		for _, name := range tlsFlags {
			if cmd.Flags().Changed(name) {
				return browserTLSError
			}
		}
		if cmd.Name() == "version" {
			if clientOnly, _ := cmd.Flags().GetBool("client"); clientOnly {
				return nil
			}
		}
		// Inspect only the selected cluster and user before client-go tries
		// to open referenced certificate files on the browser filesystem.
		config, err := flags.ToRawKubeConfigLoader().RawConfig()
		if err != nil {
			return err
		}
		contextName := config.CurrentContext
		if *flags.Context != "" {
			contextName = *flags.Context
		}
		clusterName, userName := *flags.ClusterName, *flags.AuthInfoName
		if ctx := config.Contexts[contextName]; ctx != nil {
			if clusterName == "" {
				clusterName = ctx.Cluster
			}
			if userName == "" {
				userName = ctx.AuthInfo
			}
		}
		if c := config.Clusters[clusterName]; c != nil {
			// Like client-go, discard the inherited TLS server name when --server replaces the endpoint.
			inheritsServerName := *flags.APIServer == "" && c.TLSServerName != ""
			if inheritsServerName || c.InsecureSkipTLSVerify || c.CertificateAuthority != "" || len(c.CertificateAuthorityData) > 0 {
				return browserTLSError
			}
		}
		if u := config.AuthInfos[userName]; u != nil && (u.ClientCertificate != "" || len(u.ClientCertificateData) > 0 || u.ClientKey != "" || len(u.ClientKeyData) > 0) {
			return browserTLSError
		}
		return nil
	}
}

func main() {
	streams := genericiooptions.IOStreams{In: os.Stdin, Out: os.Stdout, ErrOut: os.Stderr}
	root := &cobra.Command{Use: "kubectl"}
	flags := genericclioptions.NewConfigFlags(true)
	flags.WrapConfigFn = func(c *rest.Config) *rest.Config {
		// A custom dialer disables the js/wasm Fetch transport.
		c.Transport = &http.Transport{}
		return c
	}
	flags.AddFlags(root.PersistentFlags())
	configureBrowserTLS(root, flags)
	f := cmdutil.NewFactory(flags)
	authCmd := &cobra.Command{Use: "auth", Short: "Inspect authorization"}
	authCmd.AddCommand(auth.NewCmdCanI(f, streams), auth.NewCmdWhoAmI(f, streams))
	versionCmd := version.NewCmdVersion(f, streams)
	versionCmd.Flags().Bool("warnings-as-errors", false, "Treat version skew warnings as errors")
	root.AddCommand(
		get.NewCmdGet("kubectl", f, streams),
		describe.NewCmdDescribe("kubectl", f, streams),
		explain.NewCmdExplain("kubectl", f, streams),
		events.NewCmdEvents(f, streams),
		apiresources.NewCmdAPIResources(f, streams),
		apiresources.NewCmdAPIVersions(f, streams),
		top.NewCmdTop(f, streams),
		authCmd,
		versionCmd,
	)
	root.CompletionOptions.DisableDefaultCmd = true
	if err := root.Execute(); err != nil {
		os.Exit(1)
	}
}
