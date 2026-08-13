# SPDX-License-Identifier: Apache-2.0
"""Handler registration for the grid-builder domain."""

from __future__ import annotations


def register_all_registry_handlers(runner) -> None:
    """Register every handler with the RegistryRunner.

    Imports are deferred so concurrent module loads from the runner do not
    deadlock on the import lock.
    """
    from .retrieve.retrieve_handlers import register_handlers as reg_retrieve
    from .validate.validate_handlers import register_handlers as reg_validate

    reg_retrieve(runner)
    reg_validate(runner)
