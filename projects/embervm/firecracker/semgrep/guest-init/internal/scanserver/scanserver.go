// Package scanserver provides ListenVsock, which binds an AF_VSOCK stream
// socket and returns a net.Listener for the shim HTTP server. The
// connection-oriented RPC server that previously lived in this package has
// been retired: scan requests now arrive over the fc-invoke shim protocol
// (HTTP over vsock on vsockproto.GuestHTTPPort) instead of the legacy scan
// port newline-JSON channel.
package scanserver
