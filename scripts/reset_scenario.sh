#!/usr/bin/env bash
set -euo pipefail
kubectl delete namespace kdx-test --ignore-not-found
echo "Deleted kdx-test namespace"
