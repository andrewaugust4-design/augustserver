#!/usr/bin/env bash
set -euo pipefail

# Deploy this repo's content to its live paths on this box.
# Usage: ./deploy.sh <homepage|all|<tool>>
#   homepage   sync homepage/index.html -> /var/www/augustserver/index.html
#   <tool>     sync <tool>/app/ -> /opt/<tool>/app/ and restart the <tool> service
#   all        deploy homepage + every tool below
#
# Needs sudo (writes under /var/www and /opt, restarts systemd services).
# This is a human-run step per CLAUDE.md -- Claude Code does not invoke this
# script on its own.

cd "$(dirname "${BASH_SOURCE[0]}")"

TOOLS=(musicreview imagetools drop convert video)

deploy_homepage() {
  echo "==> homepage/index.html -> /var/www/augustserver/index.html"
  sudo cp homepage/index.html /var/www/augustserver/index.html
}

deploy_tool() {
  local tool="$1"
  local src="$tool/app"
  local dest="/opt/$tool/app"
  if [[ ! -d "$src" ]]; then
    echo "!! no $src in repo, skipping" >&2
    return 1
  fi
  echo "==> $src/ -> $dest/"
  sudo rsync -a --delete "$src/" "$dest/"
  echo "==> restarting $tool.service"
  sudo systemctl restart "$tool"
  sudo systemctl --no-pager --lines=0 status "$tool"
}

target="${1:-}"

if [[ -z "$target" ]]; then
  echo "Usage: $0 <homepage|all|${TOOLS[*]// /|}>" >&2
  exit 1
fi

case "$target" in
  homepage)
    deploy_homepage
    ;;
  all)
    deploy_homepage
    for t in "${TOOLS[@]}"; do
      deploy_tool "$t"
    done
    ;;
  *)
    if printf '%s\n' "${TOOLS[@]}" | grep -qx "$target"; then
      deploy_tool "$target"
    else
      echo "Unknown target: $target" >&2
      echo "Usage: $0 <homepage|all|${TOOLS[*]// /|}>" >&2
      exit 1
    fi
    ;;
esac

echo "Done."
