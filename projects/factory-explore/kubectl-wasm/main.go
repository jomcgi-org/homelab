package main

import (
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
