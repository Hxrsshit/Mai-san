"""Stage 5C: ChatGPT history import and personal-context ingestion.

Two layers, kept apart on purpose:

- the **raw archive** (`app.history.models`) preserves imported conversations
  as they were exported. It is historical evidence, never active memory, and
  nothing in it reaches a prompt;
- the **derived layer** reuses the existing `Memory` / entity / relationship
  machinery, so imported knowledge is retrieved, deduplicated and
  conflict-resolved by exactly the code that handles live knowledge.

Imported content is untrusted data throughout. It cannot grant a capability,
authorize a tool, execute anything, or alter an instruction.
"""
