# Compatibility shim — canonical location is core.state
# Migrate imports to: from core.state import ...
from core.state import *  # noqa: F401, F403
from core.state import SessionDB, DEFAULT_DB_PATH, SCHEMA_VERSION  # explicit for IDEs
