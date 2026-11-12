#!/usr/bin/env bash
# Apply the agreed shared-contracts layout (docs/SHARED_CONTRACTS_ARCHITECTURE.md) on a branch where Account 1
# (nexus/contracts.py) and Account 2 (nexus/contracts/__init__.py) have both been merged. Idempotent; run from the repo root.
# Verified on a local integration branch merging work/account-1-cloud-platform + work/account-2-cinematic-engine +
# work/account-3-intelligence-growth: 541 fast tests, 538 pass; the 2 Account 1 failures also fail on Account 1's own
# branch (see integration/contracts_v1/RESULT.md), the 3rd was an Account 3 test literal (fixed).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
[ -f nexus/contracts/cloud.py ] || git mv nexus/contracts.py nexus/contracts/cloud.py
[ -f nexus/contracts/media.py ] || git mv nexus/contracts/__init__.py nexus/contracts/media.py
cp "$here/contracts__init__.py" nexus/contracts/__init__.py
cp "$here/test_contracts_shared.py" tests/fast/test_contracts_shared.py
if git cat-file -e :2:tests/fast/test_contracts.py 2>/dev/null; then   # add/add conflict during the merge
  git show :2:tests/fast/test_contracts.py > tests/fast/test_contracts_cloud.py
  git show :3:tests/fast/test_contracts.py > tests/fast/test_contracts_media.py
  git rm -q --cached tests/fast/test_contracts.py; rm -f tests/fast/test_contracts.py
fi
sed -i 's#tests/fast/test_contracts.py#tests/fast/test_contracts_cloud.py#; s#nexus/contracts.py#nexus/contracts/cloud.py#' nexus/contracts/cloud.py
git add nexus/contracts tests/fast/test_contracts_*.py
python -m pytest -q tests/fast/test_contracts_shared.py tests/fast/test_contracts_cloud.py tests/fast/test_contracts_media.py tests/fast/test_intel_handoff.py
