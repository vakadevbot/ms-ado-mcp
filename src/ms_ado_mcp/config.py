import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Configuration diagnostic containing no supplied values or private source."""


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
    organization: str | None = None

    def __post_init__(self) -> None:
        if any(not valid_scope_name(p) for p in self.projects):
            raise ConfigurationError("projects must contain exact project names.")
        if len({p.casefold() for p in self.projects}) != len(self.projects):
            raise ConfigurationError("projects contains duplicate projects.")
        if self.projects:
            if self.project is None:
                object.__setattr__(self, "project", self.projects[0])
            elif self.project not in self.projects:
                raise ConfigurationError("default_project must belong to projects.")
        if any(
            len(entry.split("/")) != 2
            or not all(valid_scope_name(part) for part in entry.split("/"))
            for entry in self.allowed_repositories
        ):
            raise ConfigurationError(
                "allowed_repositories requires exact Project/Repository pairs."
            )
        if len({r.casefold() for r in self.allowed_repositories}) != len(self.allowed_repositories):
            raise ConfigurationError("allowed_repositories contains duplicate entries.")
        try:
            parsed = urlsplit(self.url)
        except ValueError:
            raise ConfigurationError("organization url is not a valid HTTPS URL.") from None
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError(
                "organization url must be an HTTPS organization/collection URL "
                "without credentials, query parameters or fragment."
            )
        if not self.pat.strip():
            raise ConfigurationError("AZURE_DEVOPS_PAT is missing or empty. Set your Azure PAT.")
        if self.api_version not in {"6.1", "6.1-preview"}:
            raise ConfigurationError("api_version must be 6.1 or 6.1-preview.")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ConfigurationError("http_timeout must be a number greater than 0 and <=300.")
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
                raise ConfigurationError("release_url must be an HTTPS collection URL.")
        if not 1 <= self.max_download_bytes <= 1073741824:
            raise ConfigurationError("max_download_bytes must be between 1 and 1073741824.")
        if any(
            not h or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in h)
            for h in self.download_hosts
        ):
            raise ConfigurationError("download_hosts must contain exact lowercase hostnames.")

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
