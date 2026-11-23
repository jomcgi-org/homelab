// forksrv-shim: LD_PRELOAD shim that makes semgrep-core a listen-after-fork server.
//
// semgrep-core opens its target file only after it has loaded and compiled its
// rules. The shim hooks open(2) and stops on the first open of FORKSRV_TARGET:
//
//   1. If FORKSRV_CHECKPOINT is not "0": write "1" to /proc/gvisor/checkpoint and
//      read "restore\n". The seed blocks in that read. Each forked child returns
//      from it with its own memory, network namespace and descriptors.
//   2. Listen on FORKSRV_LISTEN (host:port) and serve connections one at a time:
//        GET /healthz   200, then close: the wakeup probe. It does not use up
//                       the request, so the probe can poll as often as it needs.
//        POST <path>    the scan request: a Content-Length body of at most
//                       16 MiB. The shim keeps the body in a memfd and answers
//                       "200 OK" with a JSON content type.
//        anything else  an error answer, then close, and wait for the next one.
//   3. The open of the target returns the memfd instead, so the engine scans the
//      body. The engine's stdout is the connection: it writes its JSON result
//      there. When the scan ends the shim shuts the connection and the process
//      exits with the engine's own exit code: the child is single-use.
//
// No engine code changes. The request body enters the process only after the
// fork, in the child, on the child's own network.
//
// FORKSRV_CHECKPOINT=0 skips step 1 for a plain run outside the gVisor fork (the
// host smoke test of the build). FORKSRV_HOLD=1 pauses the process after the scan
// instead of exiting, so a harness can sample a live child; production sets neither.
// Progress lines go to stderr as "SHIM <what>=<CLOCK_REALTIME ns>".
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#define MAX_HEAD 16384
#define MAX_BODY ((size_t)16 << 20)
#define IO_TIMEOUT_S 10

static char subst[64];
static int resolved = 0;
static int in_child = 0;
static int net_conn = -1;

typedef int (*open_fn)(const char *, int, ...);
typedef int (*openat_fn)(int, const char *, int, ...);

static void logns(const char *what) {
  struct timespec ts;
  clock_gettime(CLOCK_REALTIME, &ts);
  char b[96];
  int l = snprintf(b, sizeof b, "SHIM %s=%lld\n", what, (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec);
  (void)!write(2, b, l);
}

static int raw_open(const char *p, int flags) { return (int)syscall(SYS_openat, AT_FDCWD, p, flags, 0); }

static void checkpoint(void) {
  int fd = raw_open("/proc/gvisor/checkpoint", O_RDWR);
  if (fd < 0) { perror("shim: open /proc/gvisor/checkpoint"); _exit(82); }
  if (write(fd, "1", 1) != 1) { perror("shim: write checkpoint"); _exit(83); }
  char res[8];
  size_t done = 0;
  while (done < sizeof res) {
    ssize_t n = read(fd, res + done, sizeof res - done);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) { fprintf(stderr, "shim: short checkpoint read n=%zd errno=%d\n", n, errno); _exit(84); }
    done += (size_t)n;
  }
  if (memcmp(res, "restore\n", sizeof res) != 0) { fprintf(stderr, "shim: unexpected checkpoint result\n"); _exit(85); }
  close(fd);
  logns("resume");
}

static int listen_on(const char *spec) {
  char host[64] = "0.0.0.0";
  const char *colon = strrchr(spec, ':');
  int port = atoi(colon ? colon + 1 : spec);
  if (colon && colon > spec && (size_t)(colon - spec) < sizeof host) {
    memcpy(host, spec, (size_t)(colon - spec));
    host[colon - spec] = 0;
  }
  struct sockaddr_in a;
  memset(&a, 0, sizeof a);
  a.sin_family = AF_INET;
  a.sin_port = htons((unsigned short)port);
  if (port <= 0 || port > 65535 || inet_pton(AF_INET, host, &a.sin_addr) != 1) {
    fprintf(stderr, "shim: bad FORKSRV_LISTEN %s\n", spec);
    _exit(86);
  }
  int ls = socket(AF_INET, SOCK_STREAM, 0);
  int one = 1;
  setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  if (bind(ls, (struct sockaddr *)&a, sizeof a) < 0 || listen(ls, 16) < 0) { perror("shim: bind/listen"); _exit(87); }
  return ls;
}

static void reply(int c, const char *status, const char *body) {
  char b[256];
  int l = snprintf(b, sizeof b, "HTTP/1.1 %s\r\nContent-Type: text/plain\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n%s", status, strlen(body), body);
  (void)!write(c, b, (size_t)l);
}

// Read one request head into hdr (NUL terminated). Returns the offset of the
// first body byte, or -1 if the connection ended, timed out or sent a head that
// is too long.
static long read_head(int c, char *hdr, size_t *have) {
  size_t hn = 0;
  for (;;) {
    ssize_t r = read(c, hdr + hn, MAX_HEAD - 1 - hn);
    if (r <= 0) return -1;
    hn += (size_t)r;
    hdr[hn] = 0;
    char *eoh = strstr(hdr, "\r\n\r\n");
    if (eoh) { *have = hn; return (eoh + 4) - hdr; }
    if (hn >= MAX_HEAD - 1) return -1;
  }
}

// Serve one connection. Returns the connection to hand to the engine, or -1 if
// the connection was not a scan request. On success *memfd holds the body.
static int serve_conn(int c, int *memfd) {
  static char hdr[MAX_HEAD];
  struct timeval tv = {IO_TIMEOUT_S, 0};
  setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
  size_t hn = 0;
  long off = read_head(c, hdr, &hn);
  if (off < 0) return -1;
  if (strncmp(hdr, "GET /healthz ", 13) == 0) { reply(c, "200 OK", "ok\n"); return -1; }
  if (strncmp(hdr, "POST ", 5) != 0) { reply(c, "405 Method Not Allowed", "POST a scan request\n"); return -1; }
  const char *cl = strcasestr(hdr, "\r\ncontent-length:");
  if (strcasestr(hdr, "\r\ntransfer-encoding:") || !cl) { reply(c, "411 Length Required", "send a Content-Length body\n"); return -1; }
  unsigned long long want = strtoull(cl + 17, 0, 10);
  if (want > MAX_BODY) { reply(c, "413 Payload Too Large", "body over 16 MiB\n"); return -1; }
  int mf = (int)syscall(SYS_memfd_create, "body", 0);
  if (mf < 0) { reply(c, "500 Internal Server Error", "no memfd\n"); return -1; }
  size_t got = hn - (size_t)off;
  if (got > want) got = (size_t)want;
  if (got && write(mf, hdr + off, got) != (ssize_t)got) { close(mf); return -1; }
  static char chunk[65536];
  while (got < want) {
    size_t n = (size_t)want - got < sizeof chunk ? (size_t)want - got : sizeof chunk;
    ssize_t r = read(c, chunk, n);
    if (r <= 0) { fprintf(stderr, "shim: short body read got=%zu want=%llu\n", got, want); close(mf); return -1; }
    if (write(mf, chunk, (size_t)r) != r) { close(mf); return -1; }
    got += (size_t)r;
  }
  logns("body");
  char m[64];
  int l = snprintf(m, sizeof m, "SHIM body_bytes=%zu\n", got);
  (void)!write(2, m, (size_t)l);
  *memfd = mf;
  return c;
}

static void net_request(const char *spec) {
  int ls = listen_on(spec);
  logns("listen");
  for (;;) {
    int c = accept(ls, 0, 0);
    if (c < 0) {
      if (errno == EINTR || errno == ECONNABORTED) continue;
      perror("shim: accept");
      _exit(88);
    }
    int mf = -1;
    if (serve_conn(c, &mf) < 0) { close(c); continue; }
    static const char ok[] = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n";
    (void)!write(c, ok, sizeof ok - 1);
    dup2(c, 1);
    if (c != 1) close(c);
    close(ls);
    net_conn = 1;
    snprintf(subst, sizeof subst, "/proc/self/fd/%d", mf);
    logns("accept");
    return;
  }
}

static void resolve(void) {
  const char *cp = getenv("FORKSRV_CHECKPOINT");
  if (!cp || strcmp(cp, "0") != 0) checkpoint();
  const char *spec = getenv("FORKSRV_LISTEN");
  net_request(spec ? spec : "0.0.0.0:8080");
  in_child = 1;
}

static const char *map(const char *path) {
  const char *magic = getenv("FORKSRV_TARGET");
  if (!magic || !path || strcmp(path, magic) != 0) return path;
  if (!resolved) { resolved = 1; resolve(); }
  return subst[0] ? subst : path;
}

#define HOOK_OPEN(NAME)                                              \
  int NAME(const char *path, int flags, ...) {                       \
    static open_fn real;                                             \
    if (!real) real = (open_fn)dlsym(RTLD_NEXT, #NAME);              \
    mode_t mode = 0;                                                 \
    if (flags & (O_CREAT | O_TMPFILE)) {                             \
      va_list ap;                                                    \
      va_start(ap, flags);                                           \
      mode = va_arg(ap, mode_t);                                     \
      va_end(ap);                                                    \
    }                                                                \
    return real(map(path), flags, mode);                             \
  }

#define HOOK_OPENAT(NAME)                                            \
  int NAME(int dirfd, const char *path, int flags, ...) {            \
    static openat_fn real;                                           \
    if (!real) real = (openat_fn)dlsym(RTLD_NEXT, #NAME);            \
    mode_t mode = 0;                                                 \
    if (flags & (O_CREAT | O_TMPFILE)) {                             \
      va_list ap;                                                    \
      va_start(ap, flags);                                           \
      mode = va_arg(ap, mode_t);                                     \
      va_end(ap);                                                    \
    }                                                                \
    return real(dirfd, map(path), flags, mode);                      \
  }

HOOK_OPEN(open)
HOOK_OPEN(open64)
HOOK_OPENAT(openat)
HOOK_OPENAT(openat64)

__attribute__((destructor)) static void shim_done(void) {
  if (!in_child) return;
  logns("done");
  if (net_conn >= 0) shutdown(1, SHUT_RDWR);
  if (getenv("FORKSRV_HOLD")) for (;;) pause();
}
