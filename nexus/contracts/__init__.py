"""NEXUS shared integration contracts - one package, three owners, one versioning rule.

Before this package, Account 1 shipped `nexus/contracts.py` and Account 2 shipped `nexus/contracts/__init__.py`; with
both in the tree Python imports the package and Account 1's module silently disappears. Agreed layout
(docs/SHARED_CONTRACTS_ARCHITECTURE.md):

  nexus.contracts.cloud         Account 1 - pydantic models, tag  `contract: "<Name>/v1"`   (storage, compute, events)
  nexus.contracts.media         Account 2 - dict schemas,   tag  `schema: "nexus.<Name>/1"` (render, edit, audio, QA)
  nexus.intelligence.contracts  Account 3 - dict schemas,   tag  `schema: "nexus.<Name>/1"` (research, scripts, publishing)

Rules: each owner edits only its own module; a breaking change adds a new major next to the old one; contract names are
unique per tag form (`registry()` fails the test suite on a collision); `validate_any(doc)` accepts every form so a
consumer never needs to know which account produced a document.

Backward compatibility: every name the two original modules exported is re-exported here unchanged, so
`from nexus.contracts import ProductionJobManifest` (Account 1) and `contracts.make("RenderJobManifest", ...)`
(Account 2) keep working. `VERSION`/`new_id` keep Account 1's meaning (the HTTP /contracts endpoint reports them)."""
from __future__ import annotations

from . import cloud, media
from .cloud import *  # noqa: F401,F403  (Account 1 models, VERSION, new_id, ALL, export_schemas)
from .cloud import ALL, VERSION, export_schemas, new_id  # noqa: F401
from .media import (SCHEMAS, CLASSIFICATIONS, RIGHTS_BASES, STATUSES, ContractError, artifact, check, dumps,  # noqa: F401
                    make, validate)

ARCHITECTURE_VERSION = 1
OWNERS = {"cloud": "account-1", "media": "account-2", "intelligence": "account-3"}


def _intelligence():
    from ..intelligence import contracts as intel  # lazy: keeps `import nexus.contracts` light and cycle-free
    return intel


def _tagged():
    """Account 1 models that are top-level documents (embedded value types such as ArtifactLocation carry no tag)."""
    return [m for m in cloud.ALL if "contract" in m.model_fields]


def registry() -> dict[str, dict]:
    """Every contract known to the system: {"<tag>": {"owner", "module", "name", "version", "form"}}."""
    out: dict[str, dict] = {}

    def add(tag, owner, module, name, version, form):
        if tag in out:
            raise ContractError(f"contract tag {tag} defined by {out[tag]['owner']} and {owner}")
        out[tag] = {"owner": owner, "module": module, "name": name, "version": version, "form": form}

    for m in _tagged():
        tag = m.model_fields["contract"].default
        add(tag, OWNERS["cloud"], "nexus.contracts.cloud", m.__name__, tag.rsplit("/", 1)[1], "contract")
    for n in media.SCHEMAS:
        add(f"nexus.{n}/{media.VERSION}", OWNERS["media"], "nexus.contracts.media", n, str(media.VERSION), "schema")
    intel = _intelligence()
    for n in intel.SCHEMAS:
        add(f"nexus.{n}/{intel.VERSION}", OWNERS["intelligence"], "nexus.intelligence.contracts", n, str(intel.VERSION), "schema")
    return out


def validate_any(doc: dict) -> list[str]:
    """Validate a document from any account. Empty list = valid."""
    if not isinstance(doc, dict):
        return ["document is not an object"]
    if "contract" in doc:
        tag = doc["contract"]
        model = next((m for m in _tagged() if m.model_fields["contract"].default == tag), None)
        if model is None:
            return [f"unknown contract {tag!r}"]
        try:
            model.model_validate(doc)
            return []
        except Exception as e:  # pydantic.ValidationError
            return [str(e)]
    tag = doc.get("schema")
    if not isinstance(tag, str) or "/" not in tag:
        return ["missing 'contract' or 'schema' tag"]
    name = tag.split("/")[0].removeprefix("nexus.")
    if name in media.SCHEMAS:
        return media.validate(doc)
    intel = _intelligence()
    if name in intel.SCHEMAS:
        return intel.validate(doc)
    return [f"unknown schema {tag!r}"]
