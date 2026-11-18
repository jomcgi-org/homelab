# Alert relay image

HOME-335 criterion 4 requires the alert-relay image to build in CI.
On pull requests and merge-queue candidates, the required `pr-checks` job runs
`bazel build //bazel/images:push_all --config=ci`. The job log records the
result of this build for the commit under review. See the
[CI workflow](../../../buildbuddy.yaml).

The [image index](../../../bazel/images/BUILD) includes
`//projects/factory/alert-relay/image:image.push` in `push_all`.
Building that target packages the Linux amd64 Go binary at
`/usr/local/bin/alert-relay` in the apko image. See the
[image target](image/BUILD) and [entrypoint](image/apko.yaml).

To check this criterion for the relay alone, run
`bazel test //projects/factory/alert-relay/image:image_build_test --config=ci`.
This test depends directly on the final OCI image.
