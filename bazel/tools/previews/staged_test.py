import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import staged


class StagedTest(unittest.TestCase):
    def test_publish_then_close(self):
        api = Mock()
        api.repo.return_value = {
            "build_type": "workflow",
            "html_url": "https://jomcgi-org.github.io/homelab/",
        }
        sha = "a" * 40
        pr = {
            "number": 17,
            "state": "open",
            "title": "<script>fork()</script>",
            "head": {
                "sha": sha,
                "ref": "../../other",
                "repo": {"full_name": "fork/repo"},
            },
            "base": {"ref": "main", "repo": {"full_name": staged.REPOSITORY}},
        }
        api.all.side_effect = [[pr], []]
        api.optional.return_value = self.entry(staged.FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            opened = Path(tmp) / "opened"
            closed = Path(tmp) / "closed"
            staged.snapshot(api, opened)
            api.optional.assert_called_once_with(
                f"/contents/{staged.SELECTOR}?ref={sha}"
            )
            for path in ("pr/17/index.html", f"pr/17/{sha}/index.html"):
                self.assertEqual((opened / path).read_bytes(), staged.PAGE)
            staged.snapshot(api, closed)
            self.assertEqual(list(closed.iterdir()), [closed / "index.html"])

    def entry(self, raw):
        return {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
        }

    def test_reject_unknown_data(self):
        for raw in (b'{"fixture":"live"}', staged.FIXTURE + b"<script>", b"x" * 300):
            self.assertFalse(staged.selected(self.entry(raw)))
        self.assertFalse(
            staged.selected(dict(self.entry(staged.FIXTURE), type="symlink"))
        )


if __name__ == "__main__":
    unittest.main()
