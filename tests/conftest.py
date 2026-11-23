import json
import os

import pytest

from rollout_stall.analyze import parse_time
from rollout_stall.collect import flatten

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")

#: Every fixture is written relative to this instant, so ages are deterministic.
NOW = parse_time("2026-11-23T12:00:00Z")


def fixture_path(name):
    return os.path.join(FIXTURES, name)


def load_fixture(name):
    with open(fixture_path(name), "r", encoding="utf-8") as handle:
        return flatten(json.load(handle))


@pytest.fixture
def now():
    return NOW


@pytest.fixture
def items():
    return load_fixture
