import asyncio
import os
import sys
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_handshake_tools_prompt_resource_and_validation(tmp_path):
    # Private fixture is created in a temporary directory; no live Azure access is needed.
    rules = tmp_path / "rules.py"
    rules.write_text(
        "import asyncio\n"
        "from ms_ado_mcp.models import Violation\n"
        "from ms_ado_mcp.policy import ProjectPolicy, Rule\n"
        "async def check(item, ctx):\n"
        "    print('checker started')\n"
        "    await asyncio.sleep(0.01)\n"
        "    print('checker finished')\n"
        "    return [Violation(code='demo.owner', message='Owner required')]\n"
        "PROJECTS = [ProjectPolicy(name='Demo', rules=(\n"
        "    Rule('demo.owner', 'Owner must be filled.', check),\n"
        "))]\n",
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "AZURE_DEVOPS_URL=https://example.invalid/collection\n"
        "AZURE_DEVOPS_PAT=stdio-fake-pat\n"
        "AZURE_DEVOPS_PROJECT=Demo\n"
        "ADO_RULES_FILE=rules.py\n",
    )
    # Override any developer's real connection settings in the child.
    env = {**os.environ}
    for key in list(env):
        if key.startswith(("AZURE_DEVOPS_", "ADO_")):
            del env[key]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ms_ado_mcp", "--env-file", str(dotenv)],
        cwd=str(tmp_path.parent),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=15)
        ) as session:
            init = await session.initialize()
            assert "Owner must be filled." in init.instructions
            assert "stdio-fake-pat" not in init.model_dump_json()
            catalog = await session.list_tools()
            tools = {t.name: t for t in catalog.tools}
            assert set(tools) == {
                "get_project_rules",
                "list_active_work_items",
                "search_work_items",
                "get_work_item",
                "get_work_item_hierarchy",
                "create_user_story",
                "create_task",
                "validate_work_item",
            }
            assert tools["create_task"].annotations.idempotentHint is False
            assert tools["validate_work_item"].annotations.readOnlyHint is True
            assert (
                tools["list_active_work_items"].inputSchema["properties"]["limit"]["maximum"] == 200
            )
            result = await session.call_tool("get_project_rules", {})
            assert not result.isError
            prompt = await session.get_prompt(
                "prepare_work_item", {"kind": "task", "project": "Demo"}
            )
            assert "Owner must be filled." in prompt.messages[0].content.text
            resource = await session.read_resource("ado://rules")
            assert "Owner must be filled." in resource.contents[0].text
            validation = await session.call_tool(
                "validate_work_item",
                {"kind": "task", "title": "Test task"},
            )
            assert not validation.isError
            assert validation.structuredContent["valid"] is False
            result = await session.call_tool("create_task", {"title": "Test task"})
            assert not result.isError
            assert result.structuredContent["created"] is False
            concurrent = await asyncio.gather(
                *(
                    session.call_tool("validate_work_item", {"kind": "task", "title": f"Task {n}"})
                    for n in range(3)
                )
            )
            assert all(not result.isError for result in concurrent)
            assert all(result.structuredContent["valid"] is False for result in concurrent)
            bad_id = await session.call_tool("get_work_item", {"item_id": -1})
            assert bad_id.isError
