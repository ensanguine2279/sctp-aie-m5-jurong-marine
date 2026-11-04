#!/usr/bin/env bash
# uvr.sh — unset vars named in .env, then uv run with .env
ENV_FILE="${ENV_FILE:-.env}"

while read -r v; do
  unset "$v"
done < <(tr -d '\r' < "$ENV_FILE" \
         | grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' \
         | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//')

exec uv run --env-file "$ENV_FILE" "$@"