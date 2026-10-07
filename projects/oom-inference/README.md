# oom-inference

An inference engine for Mixture-of-Experts models too big for your GPU and RAM
combined. It serves Qwen3.8-Flash-Next (125B parameters, NVFP4) from a 24 GB
GPU, 64 GB of RAM and an NVMe drive by keeping the hot experts in VRAM, warm ones
in pinned RAM and the rest on disk. The CLI is `oominf`; it speaks the OpenAI
and Anthropic APIs.

| Platform                  | Status                                                              |
| ------------------------- | ------------------------------------------------------------------- |
| Linux x86-64 + NVIDIA GPU | Supported (tested on an RTX 4090)                                   |
| macOS                     | Development build only: builds and runs the CPU tests, cannot serve |
| Windows                   | Untested                                                            |

**Start here: [Quickstart](docs/guide/quickstart.md)**

| Guide                                               |                                                |
| --------------------------------------------------- | ---------------------------------------------- |
| [Install](docs/guide/install.md)                    | Build on Linux, macOS or Windows               |
| [Configuration](docs/guide/configuration.md)        | The flags worth changing                       |
| [API](docs/guide/api.md)                            | Endpoints, streaming, tool calls, clients      |
| [Hardware sizing](docs/guide/hardware.md)           | What your machine will get                     |
| [Troubleshooting](docs/guide/troubleshooting.md)    | Start-up errors, warnings, bug reports         |
| [Contributing and how it works](docs/dev/README.md) | Architecture and the reasons behind it, testing, measurements |
