# gazelle:ignore
#
# app.py is a zip-lane FIXTURE, not buildable code: it is packed into echo.zip
# (checked in) and imported guest-side by the runtime shim inside a microVM, not
# by any Bazel target. gazelle:ignore keeps gazelle from generating a py_library
# for it (which would pull the fixture into the build graph). The zip lane is
# CI-exercised by projects/embervm/control/test/embervm/zip_lane_echo_test.exs
# and the fixture is round-trip-checked against the shim; see README.md for the
# live procedure.
