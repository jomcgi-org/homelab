"""Compare real repository downloads and Bazel-produced package actions."""

import json
import os
from pathlib import Path
import subprocess
import tarfile
import unittest


def runfile(path):
    return Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / path


class CompatibilityTest(unittest.TestCase):
    def test_downloads_and_packages(self):
        paths = json.loads(runfile(os.environ["HTTP_MANIFEST"]).read_text())
        paths = {key: runfile(value) for key, value in paths.items()}
        arches = os.environ["HTTP_ARCHES"].split(",")
        goose = os.environ["HTTP_GOOSE"] == "True"
        archive = os.environ["HTTP_ARCHIVE"] == "True"
        assets = os.environ["HTTP_ASSETS"] == "True"
        package = "usr/local/bin/goose" if goose else "opt/fixture/bin/fixture"
        expected = {}
        for arch in arches:
            with self.subTest(arch=arch):
                source = paths["source_" + arch]
                if archive:
                    with tarfile.open(source) as release:
                        entry = "goose" if goose else "nested/renamed"
                        source_members = {
                            member.name.removeprefix("./"): member
                            for member in release.getmembers()
                        }
                        expected[arch] = release.extractfile(
                            source_members[entry]
                        ).read()
                        if not goose:
                            self.assertEqual(source_members[entry].mode, 0o644)
                else:
                    expected[arch] = source.read_bytes()
                self.assertEqual(paths[arch].read_bytes(), expected[arch])
                self.assertTrue(os.access(paths[arch], os.X_OK), paths[arch])
                with tarfile.open(paths["tar_" + arch]) as package_tar:
                    members = {
                        member.name.removeprefix("./"): member
                        for member in package_tar.getmembers()
                    }
                    self.assertEqual(members[package].mode, 0o755)
                    self.assertEqual(
                        package_tar.extractfile(members[package]).read(), expected[arch]
                    )
                    if assets:
                        self.assertEqual(
                            package_tar.extractfile(
                                members["opt/fixture/bin/themes/theme.json"]
                            ).read(),
                            b'{"fixture":true}\n',
                        )
                        self.assertEqual(
                            package_tar.extractfile(
                                members["opt/fixture/bin/config.txt"]
                            ).read(),
                            b"fixture config\n",
                        )
                print(
                    f"PASS {arch}: source bytes, raw executable mode, package bytes/path/0755"
                )
        preferred = "amd64" if "amd64" in arches else "arm64"
        self.assertEqual(paths["binary"].read_bytes(), expected[preferred])
        self.assertTrue(os.access(paths["binary"], os.X_OK))
        self.assertEqual(
            paths["tar"].read_bytes(), paths["tar_" + preferred].read_bytes()
        )
        if not goose:
            result = subprocess.run(
                [str(paths["binary"])], check=True, capture_output=True, text=True
            )
            self.assertEqual(result.stdout, "fixture-" + preferred + "\n")
            print("PASS :binary execution and architecture-preferred aliases")
        if "metadata" in paths:
            self.assertIn("multiarch_tar", paths["metadata"].read_text())


if __name__ == "__main__":
    unittest.main()
