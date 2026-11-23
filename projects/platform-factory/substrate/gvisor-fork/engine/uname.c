// A minimal uname(1): -s (the default), -m and -r, from uname(2).
//
// semgrep-core runs `uname -s` at startup (its HTTP client library asks which
// OS it runs on) and stops if the command is missing. The distroless base image
// has no coreutils, so the image carries this instead.
#include <stdio.h>
#include <string.h>
#include <sys/utsname.h>

int main(int argc, char **argv) {
  struct utsname u;
  if (uname(&u) != 0) { perror("uname"); return 1; }
  const char *field = u.sysname;
  if (argc == 2 && strcmp(argv[1], "-m") == 0) field = u.machine;
  else if (argc == 2 && strcmp(argv[1], "-r") == 0) field = u.release;
  else if (argc > 2 || (argc == 2 && strcmp(argv[1], "-s") != 0)) { fprintf(stderr, "usage: uname [-s|-m|-r]\n"); return 64; }
  puts(field);
  return 0;
}
