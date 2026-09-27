#!/usr/bin/env bash
# Build cdnextract into ./publish/cdnextract (self-contained single file, no
# .NET runtime needed where it runs). Needs the .NET 10 SDK: either on PATH or
# user-local in ~/.dotnet (curl -sSL https://dot.net/v1/dotnet-install.sh |
# bash -s -- --channel 10.0). deploy.sh runs this before syncing wow/.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
DOTNET="$(command -v dotnet || echo "$HOME/.dotnet/dotnet")"
[[ -x "$DOTNET" ]] || { echo "!! .NET SDK not found (see build.sh header)" >&2; exit 1; }
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
"$DOTNET" publish -c Release -o publish --nologo -v quiet
echo "built $(pwd)/publish/cdnextract"
