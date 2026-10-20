import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    url: str
    pat: str = field(repr=False)
    project: str | None = None
    rules_file: Path | None = None
    api_version: str = "6.1-preview"
    timeout: float = 30
    ca_bundle: Path | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("AZURE_DEVOPS_URL must be an HTTPS organization/collection URL.")
        if not self.pat.strip():
            raise ValueError("AZURE_DEVOPS_PAT is required.")
        if self.api_version not in {"6.1", "6.1-preview"}:
            raise ValueError("AZURE_DEVOPS_API_VERSION must be 6.1 or 6.1-preview.")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("ADO_HTTP_TIMEOUT must be in (0, 300].")

    def version(self, endpoint: str) -> str:
        if self.api_version == "6.1":
            return "6.1"
        revision = {"wiql": 2, "workitems": 3, "workitemsbatch": 1, "states": 1}[endpoint]
        return f"6.1-preview.{revision}"


def load_settings(env_file: Path) -> Settings:
    env_file = env_file.expanduser().resolve()
    # Do not search parent directories or mutate the process environment.
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}

    def path(key: str) -> Path | None:
        value = values.get(key)
        if not value:
            return None
        result = Path(value).expanduser()
        return (env_file.parent / result).resolve()

    return Settings(
        url=(values.get("AZURE_DEVOPS_URL") or "").rstrip("/"),
        pat=values.get("AZURE_DEVOPS_PAT") or "",
        project=values.get("AZURE_DEVOPS_PROJECT") or None,
        rules_file=path("ADO_RULES_FILE"),
        api_version=values.get("AZURE_DEVOPS_API_VERSION") or "6.1-preview",
        timeout=float(values.get("ADO_HTTP_TIMEOUT") or "30"),
        ca_bundle=path("ADO_CA_BUNDLE"),
    )
