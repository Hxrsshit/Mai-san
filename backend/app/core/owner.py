"""Stage 6M.4: the one configured owner.

Mai is single-owner. This is the only definition of that owner's id; every
other name for it (`app.tasks.models.LOCAL_OWNER_ID`, the 6K composition's
default, the background delivery owner) is this value, imported. Two places
resolve a caller to it, one per execution context:

- **requests**: `app.api.deps.get_owner_id`, after `app.core.auth` accepts
  the request's bearer token;
- **background work**: the runtime's work sources, which have no request and
  serve the configured owner directly (`app.background.deliveries`).

The value is unchanged from Stage 6A, so every existing row keeps its owner.
"""

import uuid

#: The configured owner. Not configurable at runtime, and never taken from a
#: request: a caller is authenticated *as* this owner, it does not name one.
CONFIGURED_OWNER_ID = uuid.UUID("00000000-0000-0000-0000-00000000a1a1")

__all__ = ["CONFIGURED_OWNER_ID"]
