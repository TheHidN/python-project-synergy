from project_synergy import hello


def test_hello() -> None:
    assert hello("world") == "Hello, world!"
