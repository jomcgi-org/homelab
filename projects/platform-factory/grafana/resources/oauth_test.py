from configparser import ConfigParser
from pathlib import Path


def test_operator_groups_receive_admin():
    config = ConfigParser(interpolation=None)
    config.read(Path(__file__).with_name("grafana.ini"))
    oauth = config["auth.generic_oauth"]
    assert oauth.getboolean("role_attribute_strict")
    assert oauth["role_attribute_path"] == (
        "(contains(groups[*], 'authentik Admins') || contains(groups[*], 'factory-operators')) && 'Admin'"
    )
