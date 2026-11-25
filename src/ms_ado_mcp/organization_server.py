"""Route tools to isolated organization runtimes; writes have one fixed global scope."""

import inspect
from contextlib import AsyncExitStack, asynccontextmanager
from functools import wraps

from mcp.server.fastmcp import FastMCP

from .app_config import AppSettings
from .config import ConfigurationError
from .models import Kind
from .policy import load_policies
from .server import READ, build_server


class ToolCatalog:
    """Collect public registration calls without depending on SDK private registries."""

    def __init__(self, name, *, instructions, lifespan):
        self.instructions = instructions
        self.lifespan = lifespan
        self.tools = {}

    def add_tool(self, fn, **options):
        name = options.get("name") or fn.__name__
        self.tools[name] = (fn, options)

    def tool(self, **options):
        def decorate(fn):
            self.add_tool(fn, **options)
            return fn

        return decorate

    def prompt(self):
        return lambda fn: fn

    def resource(self, uri):
        return lambda fn: fn


def routed_tool(name, catalogs, default_organization, write_organization):
    template, options = catalogs[default_organization].tools[name]
    read_only = options["annotations"].readOnlyHint is True

    @wraps(template)
    async def dispatch(**arguments):
        if read_only:
            organization = arguments.pop("organization", None)
            selected = default_organization if organization is None else organization
        else:
            if "organization" in arguments:
                raise ValueError("Write organization is configured by the server, not the caller.")
            selected = write_organization or default_organization
        if selected not in catalogs:
            raise ValueError("Organization is not configured; consult get_access_scope.")
        fn, _ = catalogs[selected].tools[name]
        result = fn(**arguments)
        if inspect.isawaitable(result):
            result = await result
        return result

    signature = inspect.signature(template)
    if read_only:
        signature = signature.replace(
            parameters=[
                *signature.parameters.values(),
                inspect.Parameter(
                    "organization",
                    inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=str | None,
                ),
            ]
        )
        dispatch.__doc__ = (template.__doc__ or "") + (
            "\norganization selects a configured organization/collection; omitted uses the "
            "configured default. project is a project INSIDE that organization."
        )
    dispatch.__signature__ = signature
    return dispatch, options


def build_organization_server(settings: AppSettings) -> FastMCP:
    policies = {}
    writes = []
    for name, org in settings.organizations.items():
        if org.ca_bundle is not None and not org.ca_bundle.is_file():
            raise ConfigurationError("ca_bundle must point to an existing PEM file.")
        try:
            policies[name] = load_policies(org.rules_file, org.project, org.projects)
        except ValueError:
            raise ConfigurationError(
                "Cannot load private rules_file; check PROJECTS and BoardScope. "
                "No fallback is allowed."
            ) from None
        writes.extend((name, p.name) for p in policies[name].values() if p.write_scope is not None)
    if len(writes) > 1:
        raise ConfigurationError("Only one BoardScope may enable writes across ALL organizations.")

    catalogs = {
        name: build_server(org, policies[name], server_factory=ToolCatalog)
        for name, org in settings.organizations.items()
    }

    @asynccontextmanager
    async def lifespan(server):
        async with AsyncExitStack() as stack:
            for catalog in catalogs.values():
                await stack.enter_async_context(catalog.lifespan(server))
            yield None

    instructions = (
        "Organization/collection -> project -> repository are separate scopes. "
        "Use get_access_scope for configured organizations, their projects and allowed "
        "Organization/Project/Repository paths. Select organization explicitly when needed. "
        "An ID never grants access outside those scopes. All writes use the single global "
        "BoardScope; write tools accept no organization or project routing. "
        + "\n\n".join(
            f"Organization: {name}\n{catalog.instructions}" for name, catalog in catalogs.items()
        )
    )
    server = FastMCP("ms-ado-mcp", instructions=instructions, lifespan=lifespan)
    for name in catalogs[settings.default_organization].tools:
        if name == "get_access_scope":
            continue
        fn, options = routed_tool(
            name, catalogs, settings.default_organization, writes[0][0] if writes else None
        )
        # Use the wrapper's augmented description rather than a stale explicit one.
        options = {**options, "description": fn.__doc__}
        server.add_tool(fn, **options)

    @server.tool(annotations=READ)
    def get_access_scope() -> dict:
        """Configured organization/project/repository scopes, not a probe of PAT permissions."""
        return {
            "default_organization": settings.default_organization,
            "write_scope": {"organization": writes[0][0], "project": writes[0][1]}
            if writes
            else None,
            "organizations": [
                {
                    "name": name,
                    "url": org.url,
                    "default_project": org.project,
                    "projects": [
                        {
                            "name": p,
                            "repositories": list(org.repositories_for(p)),
                            "repository_paths": [
                                f"{name}/{p}/{r}" for r in org.repositories_for(p)
                            ],
                        }
                        for p in org.projects
                    ],
                }
                for name, org in settings.organizations.items()
            ],
        }

    @server.prompt()
    def prepare_work_item(
        kind: Kind, project: str | None = None, organization: str | None = None
    ) -> str:
        selected = settings.default_organization if organization is None else organization
        if selected not in policies:
            raise ValueError("Organization is not configured.")
        project = project or settings.organizations[selected].project
        if project not in policies[selected]:
            raise ValueError("Project is not configured in this organization.")
        return policies[selected][project].instructions(kind)

    @server.resource("ado://rules")
    def project_rules() -> str:
        return "\n\n".join(
            f"Organization: {name}\n{p.instructions()}"
            for name, group in policies.items()
            for p in group.values()
        )

    return server
