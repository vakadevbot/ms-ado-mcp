import json
import os
import sys

import pytest

from ms_ado_mcp.server import main


@pytest.mark.parametrize(
    "case,message",
    [
        ("pat", "AZURE_DEVOPS_PAT is missing"),
        ("old-env", "move other settings to JSON"),
        ("url", "organization url must be"),
        ("timeout", "Invalid JSON config"),
        ("version", "Invalid JSON config"),
        ("rules", "Cannot load private rules_file"),
        ("ca", "ca_bundle must point"),
        ("projects", "Invalid JSON config"),
        ("secret-json", "Invalid JSON config"),
    ],
)
def test_startup_diagnostics(tmp_path, monkeypatch, capsys, case, message):
    for key in os.environ:
        if key.startswith(("AZURE_DEVOPS_", "ADO_")):
            monkeypatch.delenv(key)
    env = tmp_path / ".env"
    env.write_text("AZURE_DEVOPS_PAT=private-secret\n")
    org = {"name": "example", "url": "https://ado.example/collection", "projects": ["Demo"]}
    data = {"organizations": [org]}
    if case == "pat":
        env.write_text("AZURE_DEVOPS_PAT=\n")
    elif case == "old-env":
        env.write_text("AZURE_DEVOPS_PAT=private-secret\nADO_RULES_FILE=private-secret.py\n")
    elif case == "url":
        org["url"] = "http://user:private-secret@ado.example"
    elif case == "timeout":
        data["http_timeout"] = "private-secret"
    elif case == "version":
        data["api_version"] = "private-secret"
    elif case == "rules":
        org["rules_file"] = "missing.py"
    elif case == "ca":
        data["ca_bundle"] = "missing.pem"
    elif case == "projects":
        org["projects"] = []
    else:
        data["pat"] = "private-secret"
    config = tmp_path / "config.json"
    config.write_text(json.dumps(data))
    monkeypatch.setattr(sys, "argv", ["ms-ado-mcp", "--config-file", str(config)])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert message in output.err and str(config) in output.err and str(env) in output.err
    assert "private-secret" not in output.err
    log = (tmp_path / "logs" / "ms-ado-mcp.log").read_text(encoding="utf-8")
    assert message in log and "private-secret" not in log


def test_missing_config_does_not_fall_back_to_old_env(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "AZURE_DEVOPS_URL=https://ado.example\nAZURE_DEVOPS_PAT=private-secret\n"
        "AZURE_DEVOPS_PROJECT=Demo\n"
    )
    monkeypatch.setattr(sys, "argv", ["ms-ado-mcp"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    output = capsys.readouterr()
    assert f"Config file: {tmp_path / 'config.json'} (not found)" in output.err
    assert "private-secret" not in output.err


def test_private_rule_exception_is_not_exposed(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AZURE_DEVOPS_PAT", raising=False)
    (tmp_path / "rules.py").write_text("raise ValueError('private-secret-source')\n")
    env_dir = tmp_path / "secrets"
    env_dir.mkdir()
    env = env_dir / ".env"
    env.write_text("AZURE_DEVOPS_PAT=private-secret-pat\n")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "organizations": [
                    {
                        "name": "example",
                        "url": "https://ado.example/c",
                        "projects": ["Demo"],
                        "rules_file": "rules.py",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ms-ado-mcp",
            "--config-file",
            str(config),
            "--env-file",
            str(env),
        ],
    )
    with pytest.raises(SystemExit):
        main()
    output = capsys.readouterr()
    assert output.out == "" and "Cannot load private rules_file" in output.err
    assert "private-secret" not in output.err
    assert not (env_dir / "logs").exists()
    log = (tmp_path / "logs" / "ms-ado-mcp.log").read_text(encoding="utf-8")
    assert "private-secret" not in log
