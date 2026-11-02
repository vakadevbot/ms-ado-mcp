import sys

import pytest

from ms_ado_mcp.server import main


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("AZURE_DEVOPS_PAT=\n", "AZURE_DEVOPS_PAT is missing or empty"),
        ("AZURE_DEVOPS_URL=http://user:secret@example.org\n", "AZURE_DEVOPS_URL must be"),
        ("ADO_HTTP_TIMEOUT=private-secret\n", "ADO_HTTP_TIMEOUT must be a number"),
        ("AZURE_DEVOPS_API_VERSION=private-secret\n", "AZURE_DEVOPS_API_VERSION must be"),
        ("ADO_RULES_FILE=missing.py\n", "ADO_RULES_FILE does not point to an existing file"),
        ("ADO_CA_BUNDLE=missing.pem\n", "ADO_CA_BUNDLE does not point to an existing PEM file"),
        ("AZURE_DEVOPS_PROJECT=\n", "Cannot load project rules"),
    ],
)
def test_startup_diagnostics(tmp_path, monkeypatch, capsys, extra, message):
    import os

    for key in os.environ:
        if key.startswith(("AZURE_DEVOPS_", "ADO_")):
            monkeypatch.delenv(key)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZURE_DEVOPS_URL=https://example.org\n"
        "AZURE_DEVOPS_PAT=private-secret\n"
        "AZURE_DEVOPS_PROJECT=Demo\n" + extra,
    )
    monkeypatch.setattr(sys, "argv", ["ms-ado-mcp", "--env-file", str(env_file)])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert message in output.err
    assert str(env_file) in output.err
    assert "(found)" in output.err
    assert "private-secret" not in output.err
    assert "user:secret" not in output.err


def test_missing_env_file_reports_resolved_path(tmp_path, monkeypatch, capsys):
    import os

    for key in os.environ:
        if key.startswith(("AZURE_DEVOPS_", "ADO_")):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["ms-ado-mcp", "--env-file", "missing.env"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    assert f"Env file: {tmp_path / 'missing.env'} (not found)" in capsys.readouterr().err


def test_private_rule_exception_is_not_exposed(tmp_path, monkeypatch, capsys):
    import os

    for key in os.environ:
        if key.startswith(("AZURE_DEVOPS_", "ADO_")):
            monkeypatch.delenv(key)
    (tmp_path / "rules.py").write_text("raise ValueError('private-secret-source')\n")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AZURE_DEVOPS_URL=https://example.org\n"
        "AZURE_DEVOPS_PAT=private-secret-pat\n"
        "ADO_RULES_FILE=rules.py\n",
    )
    monkeypatch.setattr(sys, "argv", ["ms-ado-mcp", "--env-file", str(env_file)])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "Cannot load project rules" in output.err
    assert "private-secret" not in output.err
