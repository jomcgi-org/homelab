"""Wheel inputs for the manifest generated on Linux with CPython 3.13.

Kept outside the Python package so ordinary tests can check the filter while
using --deleted_packages=bazel/tools/python.
"""

# The universal lock includes these packages, but the Linux pip hub has no
# usable wheel aliases for them. Keep the full lock in the integrity hash.
# python_manifest_wiring_test checks this list against the lock's PEP 508 markers.
_INAPPLICABLE = [
    "colorama",  # sys_platform == 'win32'
    "httpx2_jsfetch",  # sys_platform == 'emscripten'
    "pywin32",  # sys_platform == 'win32'
    "pywin32_ctypes",  # sys_platform == 'win32'
]

def linux_wheels(wheels_by_package):
    """Exclude only packages whose lock markers reject the generation host."""
    return [wheel for name, wheel in wheels_by_package.items() if name not in _INAPPLICABLE]
