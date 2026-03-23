# Compatibility shim — canonical location is core.clock
# Migrate imports to: from core.clock import ...
from core.clock import *  # noqa: F401, F403
from core.clock import now, get_timezone, get_timezone_name, reset_cache  # explicit for IDEs
