#!/usr/bin/env bash
# Apply the agreed shared-contracts layout (docs/SHARED_CONTRACTS_ARCHITECTURE.md) AFTER the owner-approved merges of
# Account 1, Account 2 and Account 3 into one integration branch. Run from the repository root. Idempotent: a second run
# changes nothing. It never commits, pushes or merges; it stages its changes so the owner reviews `git diff --cached`.
#
# Preconditions (checked, the script stops with a message otherwise):
#   * the working tree has no unresolved merge conflicts other than tests/fast/test_contracts.py
#   * Account 1 is merged: nexus/contracts.py (or, after a previous run, nexus/contracts/cloud.py) exists
#   * Account 2 is merged: nexus/contracts/media.py exists (Account 2 >= a443238), or nexus/contracts/__init__.py
#     still holds Account 2's schemas (older Account 2 heads), which this script then moves to media.py
#   * python with the project's dependencies is on PATH (the final check runs pytest)
# Both Account 2 states and both test-file states are handled:
#   * older Account 2 heads shipped tests/fast/test_contracts.py too -> add/add conflict, split into _cloud/_media
#   * Account 2 >= a443238 ships tests/fast/test_contracts_media.py -> Account 1's test_contracts.py is renamed _cloud
# Evidence of each run: integration/contracts_v1/RESULT.md.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
die() { echo "apply.sh: $*" >&2; exit 1; }

[ -d .git ] || [ -f .git ] || die "run from the repository root"
unresolved="$(git diff --name-only --diff-filter=U | grep -v '^tests/fast/test_contracts.py$' || true)"
[ -z "$unresolved" ] || die "resolve these merge conflicts first: $unresolved"
[ -f nexus/contracts.py ] || [ -f nexus/contracts/cloud.py ] || die "Account 1 not merged (no nexus/contracts.py)"
[ -f nexus/contracts/media.py ] || grep -q "SCHEMAS" nexus/contracts/__init__.py 2>/dev/null || die "Account 2 not merged (no media contracts)"
[ -f nexus/intelligence/contracts.py ] || die "Account 3 not merged (no nexus/intelligence/contracts.py)"

# 1. Account 1's module becomes the `cloud` submodule
[ -f nexus/contracts/cloud.py ] || git mv nexus/contracts.py nexus/contracts/cloud.py
# 2. Account 2's schemas become the `media` submodule (only on Account 2 heads older than a443238)
[ -f nexus/contracts/media.py ] || git mv nexus/contracts/__init__.py nexus/contracts/media.py
# 3. the shared package __init__ (re-exports cloud + media, registry(), validate_any())
cp "$here/contracts__init__.py" nexus/contracts/__init__.py
cp "$here/test_contracts_shared.py" tests/fast/test_contracts_shared.py
# 4. tests: one file per owner
if git cat-file -e :2:tests/fast/test_contracts.py 2>/dev/null; then          # add/add conflict (older Account 2)
  git show :2:tests/fast/test_contracts.py > tests/fast/test_contracts_cloud.py
  git show :3:tests/fast/test_contracts.py > tests/fast/test_contracts_media.py
  git rm -q --cached tests/fast/test_contracts.py; rm -f tests/fast/test_contracts.py
elif [ -f tests/fast/test_contracts.py ] && [ ! -f tests/fast/test_contracts_cloud.py ]; then   # Account 1's only
  grep -q "export_schemas" tests/fast/test_contracts.py || die "tests/fast/test_contracts.py is not Account 1's contract test"
  git mv tests/fast/test_contracts.py tests/fast/test_contracts_cloud.py
fi
sed -i 's#tests/fast/test_contracts.py#tests/fast/test_contracts_cloud.py#; s#nexus/contracts.py#nexus/contracts/cloud.py#' nexus/contracts/cloud.py
git add nexus/contracts tests/fast/test_contracts_*.py
# 5. focused check (the full suite is run separately: python -m pytest tests/fast)
python -m pytest -q -p no:cacheprovider tests/fast/test_contracts_shared.py tests/fast/test_contracts_cloud.py \
  tests/fast/test_contracts_media.py tests/fast/test_intel_handoff.py
