"""Project-scoped Git reads and bounded immutable tree traversal."""

import fnmatch
import re
from typing import Literal
from uuid import UUID

from .downloads import text_content
from .read_api import clean, dates, number, page, segment

VersionType = Literal["branch", "tag", "commit"]


def version_params(version, version_type):
    if version_type not in {"branch", "tag", "commit"} or not version:
        raise ValueError("Provide an explicit version and version_type: branch, tag or commit.")
    if version_type == "commit" and not re.fullmatch(r"[0-9a-fA-F]{40}", version):
        raise ValueError("Commit version must be a full 40-character SHA.")
    return {"versionDescriptor.version": version, "versionDescriptor.versionType": version_type}


def git_path(path):
    if (
        not path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(p in {".", ".."} for p in path.split("/"))
    ):
        raise ValueError("Use an absolute repository path without traversal segments.")
    return path.rstrip("/") or "/"


class GitRead:
    def __init__(self, api, downloads):
        self.api, self.downloads = api, downloads

    def allowed(self, metadata, project):
        owner = metadata.get("project", {}).get("name")
        name = metadata.get("name")
        return (
            isinstance(owner, str)
            and owner.casefold() == project.casefold()
            and isinstance(name, str)
            and name.casefold()
            in {r.casefold() for r in self.api.settings.repositories_for(project)}
        )

    async def repositories(self, project):
        if not self.api.settings.repositories_for(project):
            return []
        data, _ = await self.api.get(project, "git", "repositories")
        return [
            self.repository_view(r, project)
            for r in data.get("value", [])
            if self.allowed(r, project)
        ]

    def repository_view(self, data, project):
        data = dict(data)
        parent = data.pop("parentRepository", None)
        if parent and self.allowed(parent, project):
            data["parentRepository"] = self.repository_view(parent, project)
        return data

    def check_pr(self, data, project):
        if "repository" in data and not self.allowed(data["repository"], project):
            raise ValueError("PR repository is outside allowed_repositories.")
        if data.get("forkSource"):
            source = data["forkSource"].get("repository") or {}
            if not self.allowed(source, project):
                raise ValueError("PR source repository is outside allowed_repositories.")

    async def list_repositories(
        self, project: str | None = None, limit: int = 100, continuation_token: str | None = None
    ) -> dict:
        """Only allowlisted repositories of this project. An empty allowlist returns no results."""
        number(limit, 200)
        project = self.api.project(project)
        items = await self.repositories(project)
        return self.api.slice(
            items, self.api.binding(self.api.project(project), items), limit, continuation_token
        )

    async def get_repository(self, repository: str, project: str | None = None) -> dict:
        """Read an allowlisted repository by name or ID; IDs never bypass the name allowlist."""
        project = self.api.project(project)
        names = self.api.settings.repositories_for(project)
        if repository.casefold() not in {name.casefold() for name in names}:
            try:
                UUID(repository)
            except ValueError:
                raise ValueError("Repository is not in allowed_repositories.") from None
            matches = [
                r
                for r in await self.repositories(project)
                if str(r.get("id", "")).casefold() == repository.casefold()
            ]
            if not matches:
                raise ValueError("Repository is not in allowed_repositories.")
        data, _ = await self.api.get(project, "git", f"repositories/{segment(repository)}")
        if not data.get("project", {}).get("name"):
            raise ValueError("Repository response lacks project ownership; access denied.")
        if not self.allowed(data, project):
            raise ValueError("Repository is not in allowed_repositories.")
        return clean(self.repository_view(data, project))

    async def repo(self, repository, project):
        data = await self.get_repository(repository, project)
        return f"repositories/{segment(data['id'])}"

    async def list_repository_refs(
        self,
        repository: str,
        project: str | None = None,
        prefix: str = "heads/",
        contains: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Branches/tags: prefix=heads/ or tags/, optionally followed by a name prefix."""
        path = await self.repo(repository, project)
        return await self.api.native_page(
            project,
            "git",
            path + "/refs",
            {"filter": prefix, "filterContains": contains},
            limit,
            continuation_token,
        )

    async def metadata(self, repo, project, path, version, version_type):
        data, _ = await self.api.get(
            project,
            "git",
            repo + "/items",
            {
                "path": git_path(path),
                "includeContentMetadata": "true",
                **version_params(version, version_type),
            },
        )
        return data

    async def list_repository_files(
        self,
        repository: str,
        version: str,
        version_type: VersionType = "branch",
        project: str | None = None,
        directory: str = "/",
        recursive: bool = False,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """List files via bounded trees; cursor pins commit SHA. No invented Git Items paging."""
        return await self.search_repository_files(
            repository,
            version,
            version_type,
            project,
            directory,
            recursive,
            limit=limit,
            continuation_token=continuation_token,
        )

    async def search_repository_files(
        self,
        repository: str,
        version: str,
        version_type: VersionType = "branch",
        project: str | None = None,
        directory: str = "/",
        recursive: bool = True,
        name: str | None = None,
        path_contains: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        extensions: list[str] | None = None,
        glob: str | None = None,
        exclude: list[str] | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Case-sensitive name/path filters. Glob/exclude use fnmatch on absolute POSIX paths;
        '*' also matches '/'. Up to 20 trees/2000 entries scanned per call; continue empty pages.
        A truncated Azure tree sets incomplete=true; missing entries cannot be recovered.
        """
        number(limit, 200)
        version_params(version, version_type)
        directory = git_path(directory)
        repo = await self.repo(repository, project)
        binding = self.api.binding(
            self.api.project(project),
            repo,
            version,
            version_type,
            directory,
            recursive,
            name,
            path_contains,
            prefix,
            suffix,
            extensions,
            glob,
            exclude,
        )
        if continuation_token:
            state = self.api.decode(continuation_token, binding)
        else:
            item = await self.metadata(repo, project, directory, version, version_type)
            if item.get("gitObjectType") != "tree" or not item.get("commitId"):
                raise ValueError("directory must resolve to a tree with a commit ID.")
            state = {
                "commit": item["commitId"],
                "queue": [[item["objectId"], directory, 0]],
                "incomplete": False,
            }
        items, requests, scanned = [], 0, 0
        while state["queue"] and len(items) < limit and requests < 20 and scanned < 2000:
            tree_id, parent, offset = state["queue"][0]
            data, _ = await self.api.get(
                project, "git", repo + "/trees/" + segment(tree_id), {"recursive": "false"}
            )
            requests += 1
            state["incomplete"] |= bool(data.get("truncated"))
            entries = data.get("treeEntries", [])
            while offset < len(entries) and len(items) < limit and scanned < 2000:
                entry = entries[offset]
                offset += 1
                scanned += 1
                leaf = entry.get("relativePath", "")
                if not leaf or "/" in leaf or "\\" in leaf or leaf in {".", ".."}:
                    raise ValueError("Invalid nonrecursive tree entry.")
                path = parent.rstrip("/") + "/" + leaf
                folder = entry.get("gitObjectType") == "tree"
                if folder and recursive:
                    if len(state["queue"]) >= 1000:
                        raise ValueError("Traversal frontier too large; narrow directory.")
                    state["queue"].append([entry["objectId"], path, 0])
                matches = (not name or name in leaf) and (
                    not path_contains or path_contains in path
                )
                matches &= not prefix or leaf.startswith(prefix)
                matches &= not suffix or leaf.endswith(suffix)
                matches &= not extensions or any(leaf.endswith(e) for e in extensions)
                matches &= not glob or fnmatch.fnmatchcase(path, glob)
                matches &= not exclude or not any(fnmatch.fnmatchcase(path, g) for g in exclude)
                if matches:
                    items.append({**entry, "path": path, "is_directory": folder})
            if offset >= len(entries):
                state["queue"].pop(0)
            else:
                state["queue"][0][2] = offset
        return page(
            clean(items),
            self.api.encode(binding, state) if state["queue"] else None,
            incomplete=state["incomplete"],
            commit=state["commit"],
            scanned_entries=scanned,
            version=version,
            version_type=version_type,
        )

    async def get_repository_file(
        self,
        repository: str,
        path: str,
        version: str,
        version_type: VersionType = "branch",
        project: str | None = None,
        start_line: int = 0,
        line_count: int = 200,
        max_bytes: int = 65536,
    ) -> dict:
        """Read UTF-8 file prefix/line range, pinned to its resolved commit. LFS is a pointer.
        Byte cap bounds network reads; ranges beyond that prefix require a local download.
        """
        number(max_bytes, 262144)
        number(start_line, 1000000, 0)
        number(line_count, 1000)
        repo = await self.repo(repository, project)
        meta = await self.metadata(repo, project, path, version, version_type)
        if meta.get("isFolder"):
            raise ValueError("Requested path is a directory.")
        if meta.get("contentMetadata", {}).get("isBinary"):
            return {
                "metadata": clean(meta),
                "binary": True,
                "text": None,
                "download_tool": "download_repository_file",
            }
        raw, _, truncated = await self.api.read_bytes(
            project,
            "git",
            repo + "/items",
            {
                "path": git_path(path),
                "download": "true",
                "resolveLfs": "false",
                **version_params(meta["commitId"], "commit"),
            },
            limit=max_bytes,
            partial=True,
            accept="application/octet-stream",
        )
        return {
            "metadata": clean(meta),
            **text_content(raw, start_line, line_count, max_bytes, truncated),
        }

    async def download_repository_file(
        self,
        repository: str,
        path: str,
        version: str,
        version_type: VersionType = "branch",
        project: str | None = None,
    ) -> dict:
        """Download a file with a generated name. LFS pointers stay pointers."""
        repo = await self.repo(repository, project)
        meta = await self.metadata(repo, project, path, version, version_type)
        if meta.get("isFolder"):
            raise ValueError("Requested path is a directory.")
        result = await self.downloads.fetch(
            project,
            "git",
            repo + "/items",
            {
                "path": git_path(path),
                "download": "true",
                "resolveLfs": "false",
                **version_params(meta["commitId"], "commit"),
            },
            ".bin",
        )
        return {**result, "commit": meta["commitId"], "lfs_resolved": False}

    async def download_repository_archive(
        self,
        repository: str,
        version: str,
        version_type: VersionType = "branch",
        project: str | None = None,
        directory: str = "/",
    ) -> dict:
        """Download a ZIP of a directory at the selected immutable commit. No extraction."""
        repo = await self.repo(repository, project)
        meta = await self.metadata(repo, project, directory, version, version_type)
        if meta.get("gitObjectType") != "tree":
            raise ValueError("directory must resolve to a tree.")
        result = await self.downloads.fetch(
            project,
            "git",
            repo + "/items",
            {
                "scopePath": git_path(directory),
                "recursionLevel": "Full",
                "$format": "zip",
                **version_params(meta["commitId"], "commit"),
            },
        )
        return {**result, "commit": meta["commitId"]}

    async def list_repository_commits(
        self,
        repository: str,
        version: str,
        since: str,
        version_type: VersionType = "branch",
        project: str | None = None,
        before: str | None = None,
        path: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Commits at a pinned version, filtered by commit date and optional path (API 6.0)."""
        number(limit, 200)
        since, before = dates(since, before)
        repo = await self.repo(repository, project)
        version_params(version, version_type)
        binding = self.api.binding(
            self.api.project(project), repo, version, version_type, since, before, path
        )
        if continuation_token:
            state = self.api.decode(continuation_token, binding)
        else:
            meta = await self.metadata(repo, project, "/", version, version_type)
            state = {"commit": meta["commitId"], "skip": 0}
        params = {
            "searchCriteria.itemVersion.version": state["commit"],
            "searchCriteria.itemVersion.versionType": "commit",
            "searchCriteria.fromDate": since,
            "$top": limit,
            "$skip": state["skip"],
        }
        if before:
            params["searchCriteria.toDate"] = before
        if path:
            params["searchCriteria.itemPath"] = git_path(path)
        data, _ = await self.api.get(project, "git", repo + "/commits", params)
        items = data.get("value", [])
        state["skip"] += len(items)
        return page(
            clean(items),
            self.api.encode(binding, state) if len(items) == limit else None,
            commit=state["commit"],
            query=params,
        )

    async def get_repository_commit(
        self,
        repository: str,
        commit: str,
        project: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Commit details and paginated changes. Use a full SHA."""
        number(limit, 200)
        version_params(commit, "commit")
        repo = await self.repo(repository, project)
        data, _ = await self.api.get(project, "git", repo + "/commits/" + commit)
        binding = self.api.binding(self.api.project(project), repo, commit)
        skip = self.api.decode(continuation_token, binding) if continuation_token else 0
        changes, _ = await self.api.get(
            project, "git", repo + "/commits/" + commit + "/changes", {"top": limit, "skip": skip}
        )
        items = changes.get("changes", [])
        return page(
            clean(items),
            self.api.encode(binding, skip + len(items)) if len(items) == limit else None,
            commit=clean(data),
        )


GIT_TOOLS = tuple(
    name for name in GitRead.__dict__ if name.startswith(("list_", "get_", "download_", "search_"))
)
