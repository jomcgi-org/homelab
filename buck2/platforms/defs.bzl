load("@prelude//platforms:defs.bzl", "host_configuration")

# Mirrors loom/platforms/defs.bzl. Branches on `project.remote_enabled`: unset
# (the homelab default today) → pure-local execution; set → BuildBuddy RE, so
# enabling RE later is a one-line .buckconfig change with no rule edits.

def _executor_config():
    if read_root_config("project", "remote_enabled", None):
        remote_only = read_root_config("project", "remote_only", None)
        return CommandExecutorConfig(
            local_enabled = True,
            remote_enabled = True,
            use_limited_hybrid = not remote_only,
            remote_execution_properties = {
                "OSFamily": "Linux",
                "container-image": "docker://gcr.io/flame-public/rbe-ubuntu24-04:latest",
            },
            remote_execution_use_case = "buck2-default",
            remote_output_paths = "output_paths",
        )
    else:
        return CommandExecutorConfig(
            local_enabled = True,
            remote_enabled = False,
        )

def _buildbuddy_platforms(ctx):
    constraints = dict()
    constraints.update(ctx.attrs.cpu_configuration[ConfigurationInfo].constraints)
    constraints.update(ctx.attrs.os_configuration[ConfigurationInfo].constraints)
    configuration = ConfigurationInfo(constraints = constraints, values = {})

    platform = ExecutionPlatformInfo(
        label = ctx.label.raw_target(),
        configuration = configuration,
        executor_config = _executor_config(),
    )

    return [
        DefaultInfo(),
        PlatformInfo(label = str(ctx.label.raw_target()), configuration = configuration),
        ExecutionPlatformRegistrationInfo(platforms = [platform]),
    ]

buildbuddy_platforms = rule(
    impl = _buildbuddy_platforms,
    attrs = {
        "cpu_configuration": attrs.dep(providers = [ConfigurationInfo]),
        "os_configuration": attrs.dep(providers = [ConfigurationInfo]),
    },
)
