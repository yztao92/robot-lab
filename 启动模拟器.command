#!/bin/zsh
cd "$(dirname "$0")" || exit 1
source scripts/launcher.sh || exit 1
robotlab_run start
