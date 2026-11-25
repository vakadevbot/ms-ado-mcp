from dataclasses import replace

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "http://ado.example/collection",
        "https://user:password@ado.example/collection",
        "https://ado.example/collection?token=secret",
        "https://ado.example/collection#fragment",
    ],
)
def test_invalid_urls(settings, url):
    with pytest.raises(ValueError):
        replace(settings, url=url)


def test_api_versions_and_configuration_checks(settings):
    assert settings.version("workitems") == "6.1-preview.3"
    assert settings.version("wiql") == "6.1-preview.2"
    assert settings.version("states") == "6.1-preview.1"
    assert replace(settings, api_version="6.1").version("states") == "6.1"
    for changes in [
        {"api_version": "7.1"},
        {"pat": ""},
        {"timeout": -1},
        {"timeout": float("nan")},
    ]:
        with pytest.raises(ValueError):
            replace(settings, **changes)
