"apko_nginx_frontend - Vite/React build to nginx apko OCI image"

load("//bazel/tools/oci:apko_image.bzl", "apko_image")

def apko_nginx_frontend(
        name,
        dist,
        config,
        contents,
        repository = None,
        visibility = ["//bazel/images:__pkg__"],
        document_root = "/usr/share/nginx/html"):
    """Create a multi-platform nginx OCI image from a Vite/React build output.

    Handles the tree artifact packaging that pkg_tar strip_prefix cannot: js_run_binary
    out_dirs produces a Bazel tree artifact whose internal paths don't match what
    strip_prefix expects, so files end up nested instead of at the document root.
    This macro uses a genrule to correctly stage the files.

    Args:
        name: The name of the image target.
        dist: Label of the js_run_binary tree artifact containing the built frontend
              (e.g., ":build" from a Vite js_run_binary with out_dirs = ["dist"]).
        config: The apko config file (should include nginx package and define both archs).
        contents: The apko contents (lock file).
        repository: The container registry repository. Defaults to standard GHCR path.
        visibility: Visibility of the generated .push target.
        document_root: Nginx document root path in the container. Default: /usr/share/nginx/html

    Creates:
        :{name} - The OCI image target
        :{name}.push - Target to push image to registry
        :{name}.run - Target to run image locally
        :{name}_lock_test - Test that verifies lock file is in sync with config

    Example:
        js_run_binary(
            name = "build",
            srcs = [":src"],
            args = ["build"],
            chdir = package_name(),
            out_dirs = ["dist"],
            tool = ":vite",
        )

        apko_nginx_frontend(
            name = "image",
            dist = ":build",
            config = "apko.yaml",
            contents = "@example_frontend_lock//:contents",
            repository = "ghcr.io/jomcgi/homelab/projects/example/frontend",
        )
    """

    # Sanitize document_root: strip leading slash for tar paths
    tar_root = document_root.lstrip("/")

    # Package tree artifact contents into the nginx document root.
    # pkg_tar strip_prefix doesn't work with tree artifacts from js_run_binary,
    # so we use a genrule with cp to get the correct path structure.
    tar_name = name + "_dist_tar"
    native.genrule(
        name = tar_name,
        srcs = [dist],
        outs = [tar_name + ".tar"],
        cmd = " && ".join([
            "STAGING=$$(mktemp -d)",
            "mkdir -p $$STAGING/" + tar_root,
            "cp -r $(RULEDIR)/dist/* $$STAGING/" + tar_root + "/",
            "tar -cf $@ -C $$STAGING " + tar_root.split("/")[0],
            "rm -rf $$STAGING",
        ]),
    )

    apko_image(
        name = name,
        config = config,
        contents = contents,
        repository = repository,
        visibility = visibility,
        tars = [":" + tar_name],
    )
