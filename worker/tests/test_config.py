from pathlib import Path

import pytest

from notbulk.config import load_config

REPO_CONFIG = str(Path(__file__).resolve().parents[2] / "config.yaml")


def test_loads_repo_config_cascade_auto_accept():
    cfg = load_config(REPO_CONFIG)
    assert cfg["cascade"]["auto_accept"] == 80
    assert cfg["cascade"]["hash_only_accept"] == 90
    assert cfg["crop"]["width"] == 734


def test_missing_file_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        load_config("/nonexistent/path/config.yaml")
