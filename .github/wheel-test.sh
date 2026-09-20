#!/bin/sh
# Run the test suite against a freshly built wheel, once per Python version,
# inside a PyPA image. Invoked by the test jobs in workflows/CI.yml that cannot
# use setup-python -- musllinux, where the runner's glibc Node cannot execute,
# and i686, where the runner is 64-bit -- which pass $VERSIONS.
#
# The interpreters live at /opt/python/<python tag>-<abi tag>/bin/python, the
# PEP 425 tag pair -- so 3.11 is cp311-cp311 but free-threaded 3.15t is
# cp315-cp315t, NOT cp315t-cp315t. There is no python on PATH in these images,
# and no uv, so each version installs with its own pip.
set -eu

: "${VERSIONS:?VERSIONS must be set by the calling workflow}"
status=0

for v in $VERSIONS; do
    abi="cp$(echo "$v" | tr -d .)"
    pytag="cp$(echo "$v" | tr -d . | tr -d t)"
    py="/opt/python/$pytag-$abi/bin/python"

    if [ ! -x "$py" ]; then
        echo "::error::no interpreter for Python $v at $py"
        status=1
        continue
    fi

    echo "::group::Python $v ($py)"
    # --no-index: the wheel under test must come from dist/, never PyPI.
    # sqlite-rs has no runtime dependencies, so nothing else needs resolving.
    "$py" -m pip install --quiet --no-index --find-links dist sqlite-rs
    # pytest directly rather than the `test` dependency group: pytest is now
    # the group's only member, and these images have no uv to resolve
    # dependency-groups with anyway.
    "$py" -m pip install --quiet pytest
    "$py" -m pytest tests/ -q || status=1
    echo "::endgroup::"
done

exit "$status"
