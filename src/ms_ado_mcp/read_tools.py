"""Explicit registration of the domain read tools, separate from work-item writes."""

from mcp.types import ToolAnnotations

from .builds import BUILD_TOOLS, Builds
from .downloads import DOWNLOAD_TOOLS, Downloads
from .git_read import GIT_TOOLS, GitRead
from .pull_requests import PR_TOOLS, PullRequests
from .read_api import ReadAPI
from .releases import RELEASE_TOOLS, Releases

READ_TOOL_NAMES = (*BUILD_TOOLS, *RELEASE_TOOLS, *GIT_TOOLS, *PR_TOOLS, *DOWNLOAD_TOOLS)


def register_read_tools(server, work_items):
    api = ReadAPI(work_items)
    downloads = Downloads(api)
    git = GitRead(api, downloads)
    for service, names in (
        (Builds(api, downloads), BUILD_TOOLS),
        (Releases(api, downloads), RELEASE_TOOLS),
        (git, GIT_TOOLS),
        (PullRequests(api, git), PR_TOOLS),
        (downloads, DOWNLOAD_TOOLS),
    ):
        for name in names:
            server.add_tool(
                getattr(service, name),
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=not name.startswith("download_"),
                ),
            )
