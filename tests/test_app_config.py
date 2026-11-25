import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from ms_ado_mcp.app_config import Configuration, load_configuration
from ms_ado_mcp.config import ConfigurationError


def configuration():
    return {
        "organizations": [
            {
                "name": "alpha",
                "url": "https://dev.azure.com/alpha",
                "projects": ["Shared", "First"],
                "rules_file": "private/rules.py",
            },
            {"name": "beta", "url": "https://dev.azure.com/beta", "projects": ["Shared", "Second"]},
        ],
        "allowed_repositories": ["alpha/Shared/api", "beta/Shared/web"],
    }


@pytest.fixture
def config_files(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith(("ADO_", "AZURE_DEVOPS_")):
            monkeypatch.delenv(key)
    path, env = tmp_path / "config.json", tmp_path / ".env"
    path.write_text(json.dumps(configuration()), encoding="utf-8")
    env.write_text("AZURE_DEVOPS_PAT=literal-${NOT_INTERPOLATED}\n", encoding="utf-8")
    return path, env


def test_three_levels_and_relative_paths(config_files, monkeypatch, tmp_path):
    path, env = config_files
    data = configuration()
    data.update(
        {
            "default_organization": "beta",
            "download_dir": "files",
            "ca_bundle": "certs/root.pem",
            "max_download_bytes": 123,
            "download_hosts": ["storage.example"],
        }
    )
    data["organizations"][1]["default_project"] = "Second"
    path.write_text(json.dumps(data))
    monkeypatch.chdir(tmp_path.parent)
    app = load_configuration(path, env)
    assert app.default_organization == "beta"
    alpha, beta = app.organizations["alpha"], app.organizations["beta"]
    assert alpha.projects == ("Shared", "First") and beta.projects == ("Shared", "Second")
    assert alpha.project == "Shared" and beta.project == "Second"
    assert alpha.repositories_for("Shared") == ("api",)
    assert beta.repositories_for("Shared") == ("web",) and beta.repositories_for("First") == ()
    assert alpha.download_dir == tmp_path / "files" and alpha.max_download_bytes == 123
    assert alpha.ca_bundle == tmp_path / "certs/root.pem"
    assert alpha.rules_file == tmp_path / "private/rules.py"
    assert alpha.download_hosts == ("storage.example",)
    assert alpha.pat == "literal-${NOT_INTERPOLATED}" and "literal-" not in repr(app)
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "process-pat")
    monkeypatch.setenv("AZURE_DEVOPS_URL", "https://other.example")
    monkeypatch.setenv("AZURE_DEVOPS_PROJECTS", "NotAllowed")
    monkeypatch.setenv("ADO_ALLOWED_REPOSITORIES", "alpha/Shared/forbidden")
    app = load_configuration(path, env)
    assert app.organizations["alpha"].pat == "process-pat"
    assert app.organizations["alpha"].url == "https://dev.azure.com/alpha"
    assert app.organizations["alpha"].repositories_for("Shared") == ("api",)


@pytest.mark.parametrize(
    "entry",
    [
        "Shared/api",
        "alpha/Shared/api/extra",
        "alpha/*/api",
        "alpha/Shared/..",
        "alpha/Shared/",
        "alpha//api",
        "other/Shared/api",
        "alpha/Second/api",
    ],
)
def test_repository_triples_fail_closed(config_files, entry):
    path, env = config_files
    data = configuration()
    data["allowed_repositories"] = [entry]
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigurationError):
        load_configuration(path, env)


@pytest.mark.parametrize(
    "change",
    [
        {"organizations": []},
        {"organizations": "alpha,beta"},
        {"default_organization": "missing"},
        {"schema_version": 2},
        {"pat": "misplaced-secret"},
        {"http_timeout": "private-secret"},
        {"max_download_bytes": -1},
        {"download_hosts": ["*.example"]},
        {"unknown_option": True},
    ],
)
def test_invalid_json_options_are_safe(config_files, change):
    path, env = config_files
    path.write_text(json.dumps({**configuration(), **change}))
    with pytest.raises(ConfigurationError) as error:
        load_configuration(path, env)
    assert "misplaced-secret" not in str(error.value) and "private-secret" not in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        {"projects": []},
        {"projects": ["Shared", "shared"]},
        {"projects": ["Bad/Name"]},
        {"projects": ["*"]},
        {"default_project": "missing"},
        {"url": "https://dev.azure.com/alpha/Shared"},
        {"url": "https://dev.azure.com/beta"},
        {"url": "https://beta.visualstudio.com"},
        {"url": "http://ado.example/collection"},
        {"url": "https://user:private-secret@ado.example/collection"},
        {"url": "https://ado.example/c?token=private-secret"},
        {"release_url": "https://vsrm.dev.azure.com/beta"},
        {"url": "https://ado.example/c/../other"},
        {"unknown": "private-secret"},
    ],
)
def test_invalid_organization_settings(config_files, change):
    path, env = config_files
    data = configuration()
    data["organizations"][0].update(change)
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigurationError) as error:
        load_configuration(path, env)
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("case", ["organization", "url", "repository", "json-key"])
def test_duplicate_config_entries_rejected(config_files, case):
    path, env = config_files
    data = configuration()
    if case == "organization":
        data["organizations"].append(deepcopy(data["organizations"][0]))
    elif case == "url":
        data["organizations"][1]["url"] = data["organizations"][0]["url"]
    elif case == "repository":
        data["allowed_repositories"].append("ALPHA/shared/API")
    else:
        path.write_text('{"organizations": [], "organizations": []}')
        with pytest.raises(ConfigurationError, match="duplicate"):
            load_configuration(path, env)
        return
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigurationError):
        load_configuration(path, env)


def test_on_prem_collection_and_release_url(config_files):
    path, env = config_files
    data = {
        "organizations": [
            {
                "name": "local-collection",
                "url": "https://ado.example/tfs/Collection",
                "projects": ["Team Project"],
            }
        ],
        "allowed_repositories": ["local-collection/Team Project/repo"],
    }
    path.write_text(json.dumps(data))
    org = load_configuration(path, env).organizations["local-collection"]
    assert org.release_base_url == "https://ado.example/tfs/Collection"
    data["organizations"][0]["release_url"] = "https://release.example/tfs/Collection"
    path.write_text(json.dumps(data))
    assert load_configuration(path, env).organizations["local-collection"].release_base_url == (
        "https://release.example/tfs/Collection"
    )


@pytest.mark.parametrize(
    "extra",
    [
        "ADO_RULES_FILE=private/rules.py",
        "AZURE_DEVOPS_URL=https://old.example",
        "ADO_DOWNLOAD_DIR=",
        "AZURE_DEVOPS_PROJECTS=Old,Other",
    ],
)
def test_env_accepts_only_token(config_files, extra):
    path, env = config_files
    env.write_text("AZURE_DEVOPS_PAT=private-secret\n" + extra)
    with pytest.raises(ConfigurationError, match="move other settings to JSON") as error:
        load_configuration(path, env)
    assert "private-secret" not in str(error.value)


def test_empty_repository_allowlist_and_missing_token(config_files, monkeypatch):
    path, env = config_files
    data = configuration()
    data.pop("allowed_repositories")
    path.write_text(json.dumps(data))
    assert all(
        not org.allowed_repositories for org in load_configuration(path, env).organizations.values()
    )
    env.unlink()
    with pytest.raises(ConfigurationError, match="AZURE_DEVOPS_PAT"):
        load_configuration(path, env)
    monkeypatch.setenv("AZURE_DEVOPS_PAT", "environment-only")
    assert load_configuration(path, env).organizations["alpha"].pat == "environment-only"


def test_example_and_schema_match_runtime(config_files):
    _, env = config_files
    root = Path(__file__).parents[1]
    app = load_configuration(root / "config.example.json", env)
    assert len(app.organizations) == 2
    assert (
        json.loads((root / "config.schema.json").read_text()) == Configuration.model_json_schema()
    )
