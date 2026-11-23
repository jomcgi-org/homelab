// forksrv-net: the entrypoint of the Pro engine image.
//
//   forksrv-net --listen HOST:PORT
//
// The same contract as ../engine/forksrv-net.c (same path in the image, same
// flag, same env rules, exec so the engine is the container's process) with
// three differences: the engine is semgrep-core-proprietary, it runs the
// Secrets product, and the launcher gives it the token that the engine checks
// at start. The engine reads SEMGREP_APP_TOKEN and only checks that a value is
// there: it does not call out with it (strace: no socket) and it does not look
// at it again after the fork. The value is the PLACEHOLDER in /semgrep/.tok. The
// real token is never in the image.
//
// Forced: LD_PRELOAD, LD_LIBRARY_PATH, FORKSRV_TARGET, FORKSRV_LISTEN,
// SEMGREP_APP_TOKEN. Kept when set by the caller: FORKSRV_CHECKPOINT (default 1;
// 0 for a plain run outside the gVisor fork) and FORKSRV_HOLD (a test hook).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// The two paths can be set at compile time for a test that runs the launcher
// outside the image; the image build sets neither.
#ifndef TOKEN_FILE
#define TOKEN_FILE "/semgrep/.tok"
#endif
#ifndef ENGINE_PATH
#define ENGINE_PATH "/semgrep/semgrep-core-proprietary"
#endif

static int read_token(char *buf, size_t size) {
  FILE *f = fopen(TOKEN_FILE, "r");
  if (!f) return -1;
  size_t n = fread(buf, 1, size - 1, f);
  fclose(f);
  buf[n] = '\0';
  buf[strcspn(buf, "\r\n")] = '\0';
  return buf[0] ? 0 : -1;
}

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
  char token[256];
  if (read_token(token, sizeof token) != 0) {
    fprintf(stderr, "forksrv-net: no placeholder token in " TOKEN_FILE "\n");
    return 78;
  }
  setenv("SEMGREP_APP_TOKEN", token, 1);
  setenv("LD_LIBRARY_PATH", "/semgrep/libs", 1);
  setenv("LD_PRELOAD", "/semgrep/shim.so", 1);
  setenv("FORKSRV_TARGET", "/scan/target.json", 1);
  setenv("FORKSRV_LISTEN", listen_spec, 1);
  setenv("FORKSRV_CHECKPOINT", "1", 0);
  char *args[] = {ENGINE_PATH, "-rules", "/semgrep/rules.json", "-targets", "/semgrep/targets.json", "-json", "-j", "1",
                  "-secrets", "-disable-secrets-validation", "-pro_intra_file", NULL};
  execv(args[0], args);
  perror("forksrv-net: exec " ENGINE_PATH);
  return 127;
}
