import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


class ConfigurationError(ValueError):
    """Configuration diagnostic containing no supplied values or private source."""


def csv_setting(value: str, name: str) -> tuple[str, ...]:
    if not value.strip():
        return ()
    result = tuple(part.strip() for part in value.split(","))
    if any(not part for part in result) or len(result) > 200:
        raise ConfigurationError(f"{name} must contain 1–200 nonempty comma-separated entries.")
    if len({part.casefold() for part in result}) != len(result):
        raise ConfigurationError(f"{name} contains duplicate entries.")
    return result


def valid_scope_name(value: str) -> bool:
    return bool(
        value
        and value == value.strip()
        and value not in {".", ".."}
        and not any(c in value for c in "/\\,*?%")
        and not any(ord(c) < 32 for c in value)
    )


@dataclass(frozen=True)
class Settings:
    url: str
    pat: str = field(repr=False)
    project: str | None = None
    rules_file: Path | None = None
    api_version: str = "6.1-preview"
    timeout: float = 30
    ca_bundle: Path | None = None
    release_url: str | None = None
    download_dir: Path | None = None
    max_download_bytes: int = 268435456
    download_hosts: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    allowed_repositories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not valid_scope_name(p) for p in self.projects):
            raise ConfigurationError("AZURE_DEVOPS_PROJECTS must contain exact project names.")
        if len({p.casefold() for p in self.projects}) != len(self.projects):
            raise ConfigurationError("AZURE_DEVOPS_PROJECTS contains duplicate projects.")
        if self.projects:
            if self.project is None:
                object.__setattr__(self, "project", self.projects[0])
            elif self.project not in self.projects:
                raise ConfigurationError(
                    "AZURE_DEVOPS_PROJECT must belong to AZURE_DEVOPS_PROJECTS."
                )
        if any(
            len(entry.split("/")) != 2
            or not all(valid_scope_name(part) for part in entry.split("/"))
            for entry in self.allowed_repositories
        ):
            raise ConfigurationError(
                "ADO_ALLOWED_REPOSITORIES requires exact Project/Repository pairs."
            )
        if len({r.casefold() for r in self.allowed_repositories}) != len(self.allowed_repositories):
            raise ConfigurationError("ADO_ALLOWED_REPOSITORIES contains duplicate entries.")
        try:
            parsed = urlsplit(self.url)
        except ValueError:
            raise ConfigurationError("AZURE_DEVOPS_URL is not a valid HTTPS URL.") from None
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError(
                "AZURE_DEVOPS_URL must be an HTTPS organization/collection URL "
                "without credentials, query parameters or fragment."
            )
        if not self.pat.strip():
            raise ConfigurationError("AZURE_DEVOPS_PAT is missing or empty. Set your Azure PAT.")
        if self.api_version not in {"6.1", "6.1-preview"}:
            raise ConfigurationError("AZURE_DEVOPS_API_VERSION must be 6.1 or 6.1-preview.")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ConfigurationError("ADO_HTTP_TIMEOUT must be a number greater than 0 and <=300.")
        if self.release_url:
            try:
                release = urlsplit(self.release_url)
                valid = (
                    release.scheme == "https"
                    and release.hostname
                    and not (
                        release.username or release.password or release.query or release.fragment
                    )
                )
            except ValueError:
                valid = False
            if not valid:
                raise ConfigurationError("ADO_RELEASE_URL must be an HTTPS collection URL.")
        if not 1 <= self.max_download_bytes <= 1073741824:
            raise ConfigurationError("ADO_MAX_DOWNLOAD_BYTES must be between 1 and 1073741824.")
        if any(
            not h or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in h)
            for h in self.download_hosts
        ):
            raise ConfigurationError("ADO_DOWNLOAD_HOSTS must contain exact lowercase hostnames.")

    @property
    def release_base_url(self) -> str:
        if self.release_url:
            return self.release_url.rstrip("/")
        parsed = urlsplit(self.url)
        if parsed.hostname == "dev.azure.com":
            return "https://vsrm.dev.azure.com" + parsed.path.rstrip("/")
        if parsed.hostname and parsed.hostname.endswith(".visualstudio.com"):
            org = parsed.hostname.split(".")[0]
            return f"https://vsrm.dev.azure.com/{org}"
        return self.url.rstrip("/")

    def repositories_for(self, project: str) -> tuple[str, ...]:
        return tuple(
            entry.split("/", 1)[1]
            for entry in self.allowed_repositories
            if entry.split("/", 1)[0].casefold() == project.casefold()
        )

    def version(self, endpoint: str) -> str:
        if endpoint == "connectiondata":
            # Location/connectionData is preview-only, even when WIT uses stable 6.1.
            return "6.1-preview.1"
        if self.api_version == "6.1":
            return "6.1"
        revision = {
            "wiql": 2,
            "workitems": 3,
            "workitemsbatch": 1,
            "states": 1,
            "connectiondata": 1,
        }[endpoint]
        return f"6.1-preview.{revision}"


def load_settings(env_file: Path) -> Settings:
    env_file = env_file.expanduser().resolve()
    # Do not search parent directories or mutate the process environment.
    try:
        # An absent file is allowed when settings are supplied through the environment.
        if env_file.exists():
            with env_file.open(encoding="utf-8") as stream:
                file_values = dotenv_values(stream=stream, interpolate=False)
        else:
            file_values = {}
    except (OSError, UnicodeError):
        raise ConfigurationError(
            "Cannot read .env; check permissions and UTF-8 encoding."
        ) from None
    values = {**file_values, **os.environ}
    try:
        timeout = float(values.get("ADO_HTTP_TIMEOUT") or "30")
    except ValueError:
        raise ConfigurationError("ADO_HTTP_TIMEOUT must be a number, for example 30.") from None
    try:
        max_download = int(values.get("ADO_MAX_DOWNLOAD_BYTES") or "268435456")
    except ValueError:
        raise ConfigurationError("ADO_MAX_DOWNLOAD_BYTES must be an integer.") from None

    def path(key: str) -> Path | None:
        value = values.get(key)
        if not value:
            return None
        result = Path(value).expanduser()
        return (env_file.parent / result).resolve()

    legacy_projects = csv_setting(values.get("AZURE_DEVOPS_PROJECT") or "", "AZURE_DEVOPS_PROJECT")
    projects = csv_setting(values.get("AZURE_DEVOPS_PROJECTS") or "", "AZURE_DEVOPS_PROJECTS")
    if projects and len(legacy_projects) > 1:
        raise ConfigurationError(
            "Use only AZURE_DEVOPS_PROJECTS for the project list; "
            "AZURE_DEVOPS_PROJECT selects one default."
        )
    if not projects and len(legacy_projects) > 1:
        projects = legacy_projects
    return Settings(
        url=(values.get("AZURE_DEVOPS_URL") or "").rstrip("/"),
        pat=values.get("AZURE_DEVOPS_PAT") or "",
        project=legacy_projects[0] if legacy_projects else None,
        projects=projects,
        allowed_repositories=csv_setting(
            values.get("ADO_ALLOWED_REPOSITORIES") or "", "ADO_ALLOWED_REPOSITORIES"
        ),
        rules_file=path("ADO_RULES_FILE"),
        api_version=values.get("AZURE_DEVOPS_API_VERSION") or "6.1-preview",
        timeout=timeout,
        ca_bundle=path("ADO_CA_BUNDLE"),
        release_url=values.get("ADO_RELEASE_URL") or None,
        download_dir=path("ADO_DOWNLOAD_DIR"),
        max_download_bytes=max_download,
        download_hosts=tuple(
            h.strip() for h in (values.get("ADO_DOWNLOAD_HOSTS") or "").split(",") if h.strip()
        ),
    )
