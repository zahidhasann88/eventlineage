"""Shared Pydantic base class for every EventLineage model."""

from __future__ import annotations

import warnings
from typing import Any

from pydantic import BaseModel, ConfigDict

# ``BaseModel.schema()`` is deprecated in Pydantic v2 (superseded by
# ``model_json_schema``) but the attribute still exists, so a field named
# ``schema`` triggers a "shadows an attribute" warning on import. ``Event`` and
# ``EventVersion`` legitimately expose ``schema``; we never call the deprecated
# method, so the warning is noise that would otherwise pollute every CLI run.
warnings.filterwarnings(
    "ignore",
    message=r'Field name "schema" in .* shadows an attribute in parent',
    category=UserWarning,
)


class EGBaseModel(BaseModel):
    """Base class for all EventLineage models.

    Conventions enforced here:

    * unknown input keys are rejected (typos in configuration are caught early),
    * enum values keep their string form on serialization so output is stable,
    * assignment is validated so partially built models cannot hold bad values.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=False,
        populate_by_name=True,
        ser_json_timedelta="iso8601",
    )

    def to_dict(self) -> dict[str, Any]:
        """Serializable dict with ``None`` values dropped (compact output)."""
        return self.model_dump(mode="json", exclude_none=True)
