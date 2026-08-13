# SPDX-License-Identifier: Apache-2.0
"""Shared library behind the grid-builder tools and handlers.

Package-unique name (`_gridbuilder_tools`, never a bare `_lib`): every
standalone domain is imported into the SAME process by
`facetwork.domains.discover_entry_point_domains()`, so a shared top-level name
would collide in `sys.modules` and the loser's handlers would fail to import.
"""
