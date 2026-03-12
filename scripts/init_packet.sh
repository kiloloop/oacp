#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <project_name> <packet_id> [--agent <agent_name>]"
  exit 1
fi

PROJECT_NAME="$1"
PACKET_ID="$2"
AGENT_NAME=""

# Parse optional --agent flag
shift 2
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) AGENT_NAME="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

OACP_ROOT="${OACP_HOME:-$HOME/oacp}"
PROJECT_ROOT="$OACP_ROOT/projects/$PROJECT_NAME"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/../templates"

if [[ ! -d "$PROJECT_ROOT" ]]; then
  echo "Error: project workspace not found: $PROJECT_ROOT"
  echo "Run: $SCRIPT_DIR/init_project_workspace.sh $PROJECT_NAME"
  exit 2
fi

REVIEW_PATH="$PROJECT_ROOT/packets/review/${PACKET_ID}.md"
FINDINGS_PATH="$PROJECT_ROOT/packets/findings/${PACKET_ID}.yaml"
MERGE_PATH="$PROJECT_ROOT/merges/${PACKET_ID}.md"

if [[ -e "$REVIEW_PATH" || -e "$FINDINGS_PATH" || -e "$MERGE_PATH" ]]; then
  echo "Error: one or more packet files already exist for packet_id=${PACKET_ID}"
  exit 3
fi

NOW_UTC=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Extract round number from packet_id (e.g., "..._r2" → 2, default 1)
ROUND=1
if [[ "$PACKET_ID" =~ _r([0-9]+)$ ]]; then
  ROUND="${BASH_REMATCH[1]}"
fi

# Copy templates
cp "$TEMPLATE_DIR/review_packet.template.md" "$REVIEW_PATH"
cp "$TEMPLATE_DIR/findings_packet.template.yaml" "$FINDINGS_PATH"
cp "$TEMPLATE_DIR/merge_decision.template.md" "$MERGE_PATH"

# Stamp metadata into review packet
sed -i '' "s/^- packet_id:$/- packet_id: ${PACKET_ID}/" "$REVIEW_PATH"
sed -i '' "s/^- created_at_utc:$/- created_at_utc: ${NOW_UTC}/" "$REVIEW_PATH"
sed -i '' "s/^- round:$/- round: ${ROUND}/" "$REVIEW_PATH"
if [[ -n "$AGENT_NAME" ]]; then
  sed -i '' "s/^- owner_agent:$/- owner_agent: ${AGENT_NAME}/" "$REVIEW_PATH"
fi

# Stamp metadata into findings packet
sed -i '' "s/^packet_id: \"\"$/packet_id: \"${PACKET_ID}\"/" "$FINDINGS_PATH"
sed -i '' "s/^source_review_packet: \"\"$/source_review_packet: \"${PACKET_ID}\"/" "$FINDINGS_PATH"
sed -i '' "s/^created_at_utc: \"\"$/created_at_utc: \"${NOW_UTC}\"/" "$FINDINGS_PATH"
sed -i '' "s/^round: 1$/round: ${ROUND}/" "$FINDINGS_PATH"

# Stamp metadata into merge decision
sed -i '' "s/^- packet_id:$/- packet_id: ${PACKET_ID}/" "$MERGE_PATH"
sed -i '' "s/^- round:$/- round: ${ROUND}/" "$MERGE_PATH"
sed -i '' "s/^- date_utc:$/- date_utc: ${NOW_UTC}/" "$MERGE_PATH"
if [[ -n "$AGENT_NAME" ]]; then
  sed -i '' "s/^- resolved_by:$/- resolved_by: ${AGENT_NAME}/" "$MERGE_PATH"
fi

echo "Created packet files (round ${ROUND}):"
echo "- $REVIEW_PATH"
echo "- $FINDINGS_PATH"
echo "- $MERGE_PATH"
