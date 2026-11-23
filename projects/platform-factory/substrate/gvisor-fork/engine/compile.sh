#!/bin/sh
# compile.sh OUT_DIR: build the shim and the launcher. Run it inside the builder
# image of pins.env (the workflow does; see build.sh). Writes OUT_DIR/forksrv-net
# (static), OUT_DIR/uname (static) and OUT_DIR/shim.so.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=${1:?usage: compile.sh OUT_DIR}
mkdir -p "$out"
flags="-O2 -Wall -Wextra -Werror -fstack-protector-strong -D_FORTIFY_SOURCE=2"
gcc $flags -shared -fPIC -Wl,-z,relro,-z,now -o "$out/shim.so" "$here/forksrv-shim.c" -ldl
gcc $flags -static -o "$out/forksrv-net" "$here/forksrv-net.c"
gcc $flags -static -o "$out/uname" "$here/uname.c"
strip "$out/shim.so" "$out/forksrv-net" "$out/uname"
