"""Complete metadata registry.

`Base.metadata` is only populated for model modules that have been imported.
Importing this module imports them all, so Alembic autogenerate and the test
schema builder both see every table.

It lives here rather than in `app.database.models.__init__` because
`app.memory.models` imports from `app.database.models.base` -- registering it
inside that package would be a circular import.
"""

from app.database.models import Base
from app.entities import models as entity_models
from app.execution import models as execution_models
from app.history import models as history_models
from app.knowledge import models as knowledge_models
from app.memory import models as memory_models
from app.relationships import models as relationship_models

# memory_models is re-exported rather than merely imported: the import exists
# for its table-registration side effect, and naming it here keeps that
# explicit instead of looking like a stray import.
__all__ = [
    "Base",
    "memory_models",
    "entity_models",
    "relationship_models",
    "knowledge_models",
    "execution_models",
    "history_models",
]
