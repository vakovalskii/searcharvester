#!/usr/bin/env bash
# Run the adapter tests in the adapter image against the WORKING TREE (mounted
# read-only at /repo), so the static checks see the current config, skills and
# compose file, not the copy baked into the image.
#
#   bash scripts/test.sh            # all tests
#   bash scripts/test.sh -k guard   # extra pytest args
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="${IMAGE:-ghcr.io/vakovalskii/searcharvester:latest}"
exec docker run --rm \
  -v "$PWD:/repo:ro" \
  -e REPO_ROOT=/repo -e PYTHONDONTWRITEBYTECODE=1 \
  -w /repo/simple_tavily_adapter \
  --entrypoint python \
  "$IMAGE" -m pytest -q -p no:cacheprovider -p no:warnings tests "$@"
