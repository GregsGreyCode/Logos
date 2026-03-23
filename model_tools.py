# Compatibility shim — canonical location is core.model_tools
# Migrate imports to: from core.model_tools import ...
from core.model_tools import *  # noqa: F401, F403
from core.model_tools import (  # explicit for IDEs
    get_tool_definitions,
    handle_function_call,
    TOOL_TO_TOOLSET_MAP,
    TOOLSET_REQUIREMENTS,
    _AGENT_LOOP_TOOLS,
    _last_resolved_tool_names,
    get_all_tool_names,
    get_toolset_for_tool,
    get_available_toolsets,
    check_toolset_requirements,
    check_tool_availability,
)
