"""Composition roots: where Mai's existing parts are assembled, and nothing else.

A module here builds long-lived object graphs from components defined
elsewhere. It defines no behaviour of its own -- no policy, no decision about
*when* anything happens -- so that every rule stays in the component that owns
it and this package can only ever be wiring.
"""
