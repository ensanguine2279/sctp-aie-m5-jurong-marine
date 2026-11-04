#!/usr/bin/env bash
# print-env.sh — print current values of variables named in .env
  
ENV_FILE="${1:-.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "File not found: $ENV_FILE" >&2
  exit 1
fi

tr -d '\r' < "$ENV_FILE" \
  | grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' \
  | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' \
  | while read -r v; do
      if [ -n "${!v+x}" ]; then
        echo "$v=${!v}"
      else
        echo "$v is not set"
      fi
    done