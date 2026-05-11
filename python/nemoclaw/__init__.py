# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NemoClaw — blueprint orchestration and sandbox management SDK."""

from __future__ import annotations

try:
    from importlib.metadata import version

    __version__ = version("nemoclaw")
except Exception:
    __version__ = "0.0.0"

__all__ = ["__version__"]
