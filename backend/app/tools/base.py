"""The contract a future tool must satisfy.

A Stage 4C tool is a **declaration**, not an implementation. It states what it
is (`definition`) and what arguments it would accept (`arguments_model`), and
that is the whole surface.

There is deliberately **no `execute` method** -- not abstract, not private,
not raising `NotImplementedError`. The specification permits an abstract
execute that stays "abstract or inaccessible"; absence is stronger than
either, and it is what makes "no general execution dispatcher exists" a fact
about the code rather than a property of the call graph. A dispatcher cannot
be written against a method that does not exist, and a test asserting no tool
class defines one cannot be satisfied by accident.

Stage 4D adds execution. It will have to add the method too, deliberately, and
in the same change that adds whatever gates it.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, ConfigDict, ValidationError

from app.tools.schemas import ToolDefinition


class ToolArguments(BaseModel):
    """Base class for a tool's argument schema.

    `extra="forbid"`, unlike the `extra="ignore"` used for model output
    elsewhere in the codebase. The difference is deliberate and the reasoning
    is worth stating:

    Elsewhere, a model inventing a field is noise to be dropped -- the caller
    wants the fields it asked for and nothing else. Here, an unexpected
    argument means the proposal and the tool disagree about what is being
    asked for, and silently dropping it would run a *different* action from
    the one proposed. Refusing is the only safe reading.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ArgumentValidationError(Exception):
    """Arguments did not match the tool's schema.

    `fields` names what failed. The offending *values* are never carried:
    they come from model output and may contain anything.
    """

    def __init__(self, fields: Optional[list] = None) -> None:
        super().__init__("arguments failed validation")
        self.fields = sorted(fields or [])


class Tool(ABC):
    """A declared capability.

    Subclasses supply metadata and an argument schema. They supply nothing
    else, because there is nothing else to supply.
    """

    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        """Immutable metadata. The authoritative record of risk and approval."""

    @property
    def arguments_model(self) -> Optional[Type[ToolArguments]]:
        """The schema arguments are validated against, if the tool declares one.

        `None` means the tool takes no arguments, and any argument at all is
        then a mismatch -- see `validate_arguments`.
        """
        return None

    @property
    def name(self) -> str:
        return self.definition.name

    def validate_arguments(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Check arguments against the tool's schema.

        Returns the validated, normalised arguments. Raises
        `ArgumentValidationError` otherwise.

        Runs *before* any future execution, and cannot alter the tool's
        metadata: it returns a plain dictionary and touches nothing else.
        """
        model = self.arguments_model
        if model is None:
            if arguments:
                raise ArgumentValidationError(sorted(arguments))
            return {}

        try:
            validated = model.model_validate(arguments)
        except ValidationError as exc:
            fields = {
                ".".join(str(part) for part in error["loc"])
                for error in exc.errors()
                if error.get("loc")
            }
            raise ArgumentValidationError(sorted(fields)) from exc

        return validated.model_dump()


__all__ = ["ArgumentValidationError", "Tool", "ToolArguments"]
