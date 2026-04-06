#!/usr/bin/env bash
set -euo pipefail
SCENARIO=$1
kubectl create namespace kdx-test --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$(dirname "$0")/../scenarios/$SCENARIO/" --namespace kdx-test
echo "Applied $SCENARIO to kdx-test namespace"
