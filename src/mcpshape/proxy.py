"""What one Proxy exposes: the Upstream's accepted Catalog, curated by the Proxy file."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcpshape.catalog import KINDS, Item

if TYPE_CHECKING:
    from mcpshape.catalog import Catalog
    from mcpshape.config import ProxyFile


def hidden_items(proxy: ProxyFile) -> frozenset[Item]:
    """Every item the Proxy file hides, under both resource kinds for resource keys."""
    return frozenset(
        Item(kind, name)
        for kind in KINDS
        for name, override in proxy.overrides(kind).items()
        if override.hidden
    )


def exposed_catalog(catalog: Catalog, proxy: ProxyFile) -> Catalog:
    """The Catalog as Clients of this Proxy see it."""
    return catalog.keep(hidden_items(proxy))


def orphaned_overrides(catalog: Catalog, proxy: ProxyFile) -> list[Item]:
    """Overrides whose Catalog item is gone. Kept, so a returning item keeps its curation."""
    return [
        Item(kind, name)
        for kind in ("tool", "resource", "prompt")
        for name in proxy.overrides(kind)
        if name not in catalog.items(kind)
        and (kind != "resource" or name not in catalog.resource_templates)
    ]
