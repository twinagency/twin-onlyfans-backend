"""Launch tables (0090_launch): history memory, personality and proactive engagement.

Each package defines its tables beside its code on the shared Base; this module
only registers them so schema checks see one metadata. Never create_all at runtime.
"""
from onlyfans_runtime.launch_history_memory import models as _memory, recent as _recent, recovery as _recovery  # noqa: F401
from onlyfans_runtime.launch_personality import models as _personality  # noqa: F401
from onlyfans_runtime.launch_engagement import models as _engagement  # noqa: F401
