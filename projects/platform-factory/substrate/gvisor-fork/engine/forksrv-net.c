// forksrv-net: the entrypoint of the engine image.
//
//   forksrv-net --listen HOST:PORT
//
// It sets the environment of the shim and runs semgrep-core in its place (exec,
// so semgrep-core is the container's process and its exit code is the actor's).
// The ActorTemplate names no env: an env name that looks like a secret is
// refused, and the environment here is fixed in the image on purpose.
//
// Forced: LD_PRELOAD, LD_LIBRARY_PATH, FORKSRV_TARGET, FORKSRV_LISTEN.
// Kept when set by the caller: FORKSRV_CHECKPOINT (default 1; 0 for a plain run
// outside the gVisor fork) and FORKSRV_HOLD (a test hook).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
  const char *listen_spec = "0.0.0.0:8080";
  for (int i = 1; i < argc; i++) {
    if (strcmp(argv[i], "--listen") == 0 && i + 1 < argc) {
      listen_spec = argv[++i];
    } else {
      fprintf(stderr, "usage: forksrv-net [--listen HOST:PORT]\n");
      return 64;
    }
  }
  setenv("LD_LIBRARY_PATH", "/semgrep/libs", 1);
  setenv("LD_PRELOAD", "/semgrep/shim.so", 1);
  setenv("FORKSRV_TARGET", "/scan/target.json", 1);
  setenv("FORKSRV_LISTEN", listen_spec, 1);
  setenv("FORKSRV_CHECKPOINT", "1", 0);
  char *args[] = {"/semgrep/semgrep-core", "-rules", "/semgrep/rules.json", "-targets", "/semgrep/targets.json", "-json", "-j", "1", NULL};
  execv(args[0], args);
  perror("forksrv-net: exec /semgrep/semgrep-core");
  return 127;
}
