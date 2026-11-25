"""JSON organization/project/repository policy and a PAT-only .env file."""

import io
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from .config import ConfigurationError, Settings, valid_scope_name


class OrganizationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    url: str
    projects: list[str] = Field(min_length=1, max_length=200)
    default_project: str | None = None
    rules_file: str | None = None
    release_url: str | None = None


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    json_schema: str | None = Field(default=None, alias="$schema")
    schema_version: Literal[1] = 1
    default_organization: str | None = None
    organizations: list[OrganizationConfig] = Field(min_length=1, max_length=20)
    allowed_repositories: list[str] = Field(default_factory=list, max_length=2000)
    api_version: Literal["6.1", "6.1-preview"] = "6.1-preview"
    http_timeout: float = Field(default=30, gt=0, le=300, allow_inf_nan=False)
    ca_bundle: str | None = None
    download_dir: str | None = None
    max_download_bytes: int = Field(default=268435456, ge=1, le=1073741824)
    download_hosts: list[str] = Field(default_factory=list, max_length=100)


@dataclass(frozen=True)
class AppSettings:
    default_organization: str
    organizations: dict[str, Settings]


def _read(path: Path, maximum: int, kind: str) -> str:
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ConfigurationError(f"{kind} file exceeds its size limit.")
        return raw.decode("utf-8-sig")
    except (OSError, UnicodeError):
        raise ConfigurationError(
            f"Cannot read {kind} file; check path, permissions and UTF-8."
        ) from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError("JSON config contains duplicate keys.")
        result[key] = value
    return result


def _names(names, kind):
    if any(not valid_scope_name(name) for name in names):
        raise ConfigurationError(f"{kind} must use exact names without slashes or wildcards.")
    if len({name.casefold() for name in names}) != len(names):
        raise ConfigurationError(f"Duplicate {kind} names are not allowed.")


def _organization_url(name, url, *, release=False):
    # Reject a project URL accidentally supplied as the organization base URL.
    try:
        parsed = urlsplit(url)
        parts = unquote(parsed.path).strip("/").split("/")
    except ValueError:
        raise ConfigurationError("Invalid organization URL.") from None
    if any(part in {".", "..", "_apis"} for part in parts):
        raise ConfigurationError("Organization URL must point to an organization/collection root.")
    if parsed.hostname in {"dev.azure.com", "vsrm.dev.azure.com"}:
        if len(parts) != 1 or parts[0].casefold() != name.casefold():
            raise ConfigurationError(
                "Cloud URL must contain exactly the configured organization name."
            )
        if not release and parsed.hostname != "dev.azure.com":
            raise ConfigurationError("Use dev.azure.com as the cloud organization URL.")
    elif parsed.hostname and parsed.hostname.endswith(".visualstudio.com"):
        if parsed.hostname != f"{name.casefold()}.visualstudio.com" or parsed.path.rstrip(
            "/"
        ) not in {"", "/DefaultCollection"}:
            raise ConfigurationError("Legacy cloud URL does not match the configured organization.")


def load_configuration(config_file: Path, env_file: Path) -> AppSettings:
    config_file = config_file.expanduser().resolve()
    env_file = env_file.expanduser().resolve()
    try:
        raw = json.loads(
            _read(config_file, 1048576, "JSON config"), object_pairs_hook=_unique_object
        )
        config = Configuration.model_validate(raw)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ConfigurationError):
            raise
        # Pydantic/JSON errors can include a misplaced PAT. Never echo their text.
        raise ConfigurationError(
            "Invalid JSON config: check schema, field names and value types. "
            "Tokens belong only in .env, never in JSON."
        ) from None

    secrets = (
        dotenv_values(stream=io.StringIO(_read(env_file, 65536, ".env")), interpolate=False)
        if env_file.exists()
        else {}
    )
    if set(secrets) - {"AZURE_DEVOPS_PAT"}:
        raise ConfigurationError(
            ".env may contain only AZURE_DEVOPS_PAT; move other settings to JSON."
        )
    pat = os.environ.get("AZURE_DEVOPS_PAT", secrets.get("AZURE_DEVOPS_PAT") or "")
    if not pat.strip():
        raise ConfigurationError("AZURE_DEVOPS_PAT is missing or empty. Set the token in .env.")

    _names([org.name for org in config.organizations], "organization")
    by_name = {org.name.casefold(): org for org in config.organizations}
    default = config.default_organization or config.organizations[0].name
    if default not in {org.name for org in config.organizations}:
        raise ConfigurationError("default_organization must name a configured organization.")
    urls = [org.url.rstrip("/").casefold() for org in config.organizations]
    if len(set(urls)) != len(urls):
        raise ConfigurationError("Each organization must have a distinct collection URL.")
    for org in config.organizations:
        _names(org.projects, "project")
        _organization_url(org.name, org.url)
        if org.release_url:
            _organization_url(org.name, org.release_url, release=True)

    allowed: dict[str, list[str]] = {org.name: [] for org in config.organizations}
    seen = set()
    for entry in config.allowed_repositories:
        parts = entry.split("/")
        if len(parts) != 3 or not all(valid_scope_name(part) for part in parts):
            raise ConfigurationError(
                "allowed_repositories requires Organization/Project/Repository."
            )
        if entry.casefold() in seen:
            raise ConfigurationError("allowed_repositories contains duplicate entries.")
        seen.add(entry.casefold())
        org = by_name.get(parts[0].casefold())
        if org is None:
            raise ConfigurationError("Repository refers to an organization outside the allowlist.")
        projects = {p.casefold(): p for p in org.projects}
        if parts[1].casefold() not in projects:
            raise ConfigurationError(
                "Repository refers to a project outside its organization allowlist."
            )
        allowed[org.name].append(f"{projects[parts[1].casefold()]}/{parts[2]}")

    def path(value):
        if not value:
            return None
        return (config_file.parent / Path(value).expanduser()).resolve()

    result = {}
    for org in config.organizations:
        result[org.name] = Settings(
            organization=org.name,
            url=org.url.rstrip("/"),
            pat=pat,
            projects=tuple(org.projects),
            project=org.default_project,
            allowed_repositories=tuple(allowed[org.name]),
            rules_file=path(org.rules_file),
            release_url=org.release_url,
            api_version=config.api_version,
            timeout=config.http_timeout,
            ca_bundle=path(config.ca_bundle),
            download_dir=path(config.download_dir),
            max_download_bytes=config.max_download_bytes,
            download_hosts=tuple(config.download_hosts),
        )
    return AppSettings(default_organization=default, organizations=result)
