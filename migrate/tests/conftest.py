from pathlib import Path

import pytest
import yaml

MAPPING = Path(__file__).resolve().parents[1] / "mapping.yaml"


@pytest.fixture(scope="session")
def mapping():
    return yaml.safe_load(MAPPING.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def aliases(mapping):
    return {str(k).lower(): v for k, v in mapping["transforms"]["state_normalise"]["aliases"].items()}
