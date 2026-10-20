from dataclasses import replace

import pytest

from ms_ado_mcp.config import load_settings


def test_env_paths_overrides_and_literal_pat(tmp_path, monkeypatch):
    for key in (
        "AZURE_DEVOPS_URL",
        "AZURE_DEVOPS_PAT",
        "AZURE_DEVOPS_PROJECT",
        "ADO_RULES_FILE",
        "AZURE_DEVOPS_API_VERSION",
        "ADO_CA_BUNDLE",
        "ADO_HTTP_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "AZURE_DEVOPS_URL=https://example.org/collection/\n"
        "AZURE_DEVOPS_PAT=literal-${NO_INTERPOLATION}\n"
        "AZURE_DEVOPS_PROJECT=Demo\n"
        "ADO_RULES_FILE=private/rules.py\n"
        "ADO_CA_BUNDLE=certs/root.pem\n",
    )
    monkeypatch.chdir(tmp_path.parent)
    settings = load_settings(env)
    assert settings.pat == "literal-${NO_INTERPOLATION}"
    assert settings.rules_file == tmp_path / "private" / "rules.py"
    assert settings.ca_bundle == tmp_path / "certs" / "root.pem"
    assert settings.url == "https://example.org/collection"
    assert "literal-" not in repr(settings)
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "process-secret")
    assert load_settings(env).pat == "process-secret"


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
