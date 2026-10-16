#!/bin/sh
# Run pytest as the unprivileged "tester" user so archivebox' root-drop import
# guard keeps the /root-installed standalone Python stdlib readable.
# Test targets/options are passed through $PYTEST_ARGS (space-separated).
cd /mnt/f/swe/098842/project-02 || exit 1
mkdir -p /tmp/abhome
exec su tester -s /bin/sh -c "cd /mnt/f/swe/098842/project-02 && HOME=/tmp/abhome .venv/bin/python -m pytest $PYTEST_ARGS"
