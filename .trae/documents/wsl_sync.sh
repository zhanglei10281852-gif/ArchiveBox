#!/bin/sh
export PATH=$HOME/.local/bin:$PATH
cd /mnt/f/swe/098842/project-02 || exit 1
uv sync --python 3.13 --dev --no-install-package playwright
