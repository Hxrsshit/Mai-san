"""Stage 4D.1 runtime identity and authoritative system knowledge.

    Settings + live provider  ->  RuntimeFacts  ->  prompt section

Facts about *this system* are deterministic and come from configuration.
Facts about *the user* remain the memory system's job and remain untrusted
reference data. Keeping the two apart is the whole point: Mai answered
"OpenAI / GPT-4" when asked what it runs on, because nothing authoritative
had ever told it otherwise.
"""

from app.runtime.facts import UNKNOWN, build, database_dialect
from app.runtime.schemas import RuntimeFacts

__all__ = ["UNKNOWN", "RuntimeFacts", "build", "database_dialect"]
