import pytest

from agriconnect.graphs.nodes.sentinelle import ClimateSentinel


def test_format_location_and_build_context():
    agent = ClimateSentinel()
    # _format_location should return a string even for empty profile
    assert isinstance(agent._format_location({}), str)
    # _build_context with empty list returns empty string
    assert agent._build_context([]) == ""
