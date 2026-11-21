load("@rules_python//python:defs.bzl", "py_library")

exports_files(["LICENSE"])

py_library(
    name = "timestral",
    srcs = glob(["src/timestral/*.py"]),
    imports = ["src"],
    visibility = ["//visibility:public"],
    deps = [
        "@capacity_forecast_pip//huggingface_hub",
        "@capacity_forecast_pip//numpy",
        "@capacity_forecast_pip//safetensors",
        "@capacity_forecast_pip//torch",
    ],
)
