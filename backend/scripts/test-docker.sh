#!/usr/bin/env sh
set -eu

project="pulse-tests-$$"
compose() {
  docker compose -p "$project" -f docker-compose.test.yml "$@"
}
cleanup() {
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

compose run --build --rm tests "$@"