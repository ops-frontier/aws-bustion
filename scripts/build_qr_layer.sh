#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m pip install --no-deps --upgrade \
  --target "$root/terraform/.terraform/qr-layer/python" \
  -r "$root/terraform/lambda/requirements.txt"
