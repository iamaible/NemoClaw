# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Entry point for ``python -m nemoclaw``.

Prints package version and exits 0.  Serves as the default Kubernetes pod
command — override ``spec.containers[].command`` in the Helm chart to run a
custom controller or one-shot reconcile script.

Exits 1 if the nemoclaw package cannot be imported (import-time breakage).
"""

from __future__ import annotations

import sys


def main() -> None:
    try:
        import nemoclaw  # noqa: PLC0415
    except ImportError as exc:
        print(f"ERROR: cannot import nemoclaw: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)

    version = getattr(nemoclaw, "__version__", "unknown")
    print(f"nemoclaw {version}", flush=True)


if __name__ == "__main__":
    main()
