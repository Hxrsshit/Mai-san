"""Test-only integrations and tools.

Deliberately outside `app/`. Stage 4F-A ships an empty integration registry,
and a fake integration living in the application package would be one edit
away from being registered in production. Keeping it here means the shipped
catalogue cannot accidentally grow a test double.
"""
