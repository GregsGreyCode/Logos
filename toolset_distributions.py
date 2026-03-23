# Compatibility shim — canonical location is core.toolset_distributions
# Migrate imports to: from core.toolset_distributions import ...
from core.toolset_distributions import *  # noqa: F401, F403
from core.toolset_distributions import (  # explicit for IDEs
    get_distribution,
    list_distributions,
    sample_toolsets_from_distribution,
    validate_distribution,
    DISTRIBUTIONS,
)
