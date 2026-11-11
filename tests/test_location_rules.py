"""Only India (or remote open to India) for Workday; only remote open to the UAE for UAE searches."""

import pytest

from applypilot.discovery.filters import india_open, uae_open


@pytest.mark.parametrize("loc", [
    "Bengaluru, India", "Hyderabad, Telangana, India", "Remote, India", "Remote - APAC",
    "Remote - Worldwide", "Remote", "2 Locations", "Bangalore, India; Remote, Poland", "",
])
def test_india_keeps(loc):
    assert india_open(loc)


@pytest.mark.parametrize("loc", [
    "Remote, United States", "Remote - Americas", "Toronto, Ontario, Canada", "New York, NY", "Remote, UK",
])
def test_india_drops_other_countries(loc):
    assert not india_open(loc)


@pytest.mark.parametrize("loc", ["United Arab Emirates", "Remote - UAE", "Dubai, UAE", "Remote", "Abu Dhabi"])
def test_uae_keeps(loc):
    assert uae_open(loc)


@pytest.mark.parametrize("loc", ["Remote, United States", "Bengaluru, India"])
def test_uae_drops(loc):
    assert not uae_open(loc)


def test_thomson_reuters_removed():
    import yaml
    from applypilot.config import CONFIG_DIR
    employers = yaml.safe_load((CONFIG_DIR / "employers.yaml").read_text())["employers"]
    assert not any("thomson" in k.lower() or "thomson" in (v.get("name") or "").lower()
                   for k, v in employers.items())
