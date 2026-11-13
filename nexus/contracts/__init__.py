"""NEXUS shared contracts package (agreed layout, docs/SHARED_CONTRACTS_ARCHITECTURE.md on Account 3's branch):

  nexus.contracts.cloud         Account 1  (pydantic models, tag `contract: "<Name>/v1"`)
  nexus.contracts.media         Account 2  (dict schemas,    tag `schema: "nexus.<Name>/1"`)  <- this branch
  nexus.intelligence.contracts  Account 3

On the Account 2 branch alone only `media` exists, so this file re-exports it unchanged (`contracts.make(...)`,
`contracts.validate(...)` keep working). When the three accounts are merged, this file is replaced by the shared
`__init__.py` from integration/contracts_v1/apply.sh (Account 3's branch), which re-exports both cloud and media.
"""
from __future__ import annotations

from . import media  # noqa: F401
from .media import (CLASSIFICATIONS, RIGHTS_BASES, SCHEMAS, STATUSES, VERSION, ContractError, artifact,  # noqa: F401
                    check, dumps, make, new_id, validate)

OWNERS = {"media": "account-2"}
