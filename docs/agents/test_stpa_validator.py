import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("status", ["none", "designed"])
@pytest.mark.parametrize("section", ["hazards", "ucas", "unsafe_feedback"])
def test_security_requires_enforced_or_staged_control(section, status):
    guide = Path(__file__).with_name("stpa.md").read_text()
    validator = guide.split("<< 'VJQEOF'\n", 1)[1].split("\nVJQEOF", 1)[0]
    row = {"key": "action.providing", "status": status, "issue": "HOME-1"}
    row.update(
        control_action="action", guideword="providing", **{"from": "a", "to": "b"}
    )
    structure = {
        "control_actions": [{"key": "action"}],
        "nodes": [{"key": "a"}, {"key": "b"}],
    }
    lens = {"lens": "security", section: [row]}
    result = subprocess.check_output(
        ["jq", "-r", validator],
        input=json.dumps({"structure": structure, "lenses": [lens]}),
        text=True,
    )
    assert result.strip() == (
        "VALIDATION_ERROR: security: action.providing requires an enforced or staged-off control"
    )
