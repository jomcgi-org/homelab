# templates/buckets.yaml passes this script to the health CronJob.
# gazelle:exclude files/health.py
# templates/cloudflare-exporter.yaml passes this copy of
# //projects/platform-factory/prometheus/resources:cloudflare_exporter.py to
# its Deployment; public_test checks that the bytes match.
# gazelle:exclude files/cloudflare_exporter.py

load("//bazel/helm:defs.bzl", "helm_chart")
load("//bazel/tools/pytest:defs.bzl", "py_test")

helm_chart(
    name = "chart",
    lint = False,
    publish = True,
    visibility = ["//visibility:public"],
)

py_test(
    name = "public_test",
    srcs = ["public_test.py"],
    data = [
        ":chart",
        "//projects/factory-public/deploy:application.yaml",
        "//projects/platform-factory/prometheus/resources:cloudflare_exporter.py",
        "FAKE_RCLONE": "$(rootpath //projects/shared/helm/factory-library:fake_rclone.py)",
