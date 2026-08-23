"""GraphQL types for `model sync` — the catalog-vs-provider drift report.

The UI counterpart of `main.py model sync`. Same module does the work
(`core.model_discovery`), same rule applies: this is a **lint**, not a source of
truth. Nothing here changes the catalog; registering what it finds is a separate,
explicit mutation (`addDiscoveredModels`), so a listing can never quietly rewrite
what the app offers.
"""

from __future__ import annotations

import asyncio

import strawberry
from sqlalchemy.ext.asyncio import AsyncSession


@strawberry.type
class DiscoveredModel:
    id: str
    label: str
    provider: str
    context_window: int | None = None
    description: str | None = None
    # False when the provider says (or the name suggests) this generates speech /
    # images / music rather than text. Advisory: shown either way, but not
    # pre-selected for adding. See core/model_discovery.looks_like_chat.
    likely_chat: bool = True


@strawberry.type
class UnreachableModel:
    id: str
    reason: str


@strawberry.type
class WindowFinding:
    """A context_window the provider states that the catalog doesn't match.

    `catalog_window` is null for a backfill (catalog has None), set for a
    disagreement. `builtin` gates whether it can be applied from the UI at all —
    a built-in's window lives in compiled-in `BUILTIN_MODELS`, so fixing it is a
    code change, not a config write.
    """

    id: str
    label: str
    provider: str
    catalog_window: int | None
    provider_window: int
    builtin: bool


@strawberry.type
class ModelSyncReport:
    provider: str
    # Number of models the provider offered. 0 with `skipped` set means discovery
    # never ran; 0 without it means the provider genuinely offers nothing.
    offered: int
    # Why this provider was skipped (missing key, unreachable host, no adapter).
    # Non-null means every list below is empty for lack of data, not for lack of
    # drift — the UI must not render that as "✓ in sync".
    skipped: str | None
    probed: bool
    missing: list[str]                    # in the catalog, no longer offered
    unreachable: list[UnreachableModel]   # offered, but this credential can't call it
    windows: list[WindowFinding]
    new_models: list[DiscoveredModel]     # offered, not in the catalog

    @strawberry.field
    def clean(self) -> bool:
        """True only when discovery ran and found nothing to report."""
        return self.skipped is None and not (
            self.missing or self.unreachable or self.windows or self.new_models
        )


@strawberry.input
class DiscoveredModelInput:
    id: str
    label: str
    provider: str | None = None
    context_window: int | None = None


async def run_model_sync(
    session: AsyncSession, provider: str | None, probe: bool,
) -> list[ModelSyncReport]:
    """Discover + diff one provider, or every discoverable one.

    Discovery and probing are blocking network calls (httpx sync client, boto3,
    provider SDKs), so each provider runs in a worker thread — a sync of five
    providers must not park the event loop and stall every live subscription.
    """
    from core.model_catalog import KNOWN_PROVIDERS, available_models, is_builtin_model
    from core.model_discovery import (
        DISCOVERABLE,
        DiscoveryError,
        build_report,
        discover,
        probe as probe_model,
    )

    from .model_catalog import load_model_catalog

    if provider is not None:
        if provider not in KNOWN_PROVIDERS:
            raise ValueError(
                f"Unknown provider '{provider}' — must be one of: "
                f"{', '.join(sorted(KNOWN_PROVIDERS))}"
            )
        if provider not in DISCOVERABLE:
            raise ValueError(
                f"No discovery adapter for '{provider}' — discoverable: "
                f"{', '.join(sorted(DISCOVERABLE))}"
            )
    targets = [provider] if provider else sorted(DISCOVERABLE)

    # Hydrate the custom-model cache so available_models() (and therefore the
    # diff) sees models added at runtime.
    await load_model_catalog(session)

    out: list[ModelSyncReport] = []
    for prov in targets:
        try:
            found = await asyncio.to_thread(discover, prov)
        except DiscoveryError as exc:
            out.append(ModelSyncReport(
                provider=prov, offered=0, skipped=str(exc), probed=False,
                missing=[], unreachable=[], windows=[], new_models=[],
            ))
            continue

        report = build_report(prov, found)
        unreachable: list[UnreachableModel] = []
        if probe:
            for spec in [m for m in available_models() if m.provider == prov]:
                ok, why = await asyncio.to_thread(probe_model, spec.id)
                if not ok:
                    unreachable.append(UnreachableModel(id=spec.id, reason=why))

        catalog = {m.id: m for m in available_models()}
        windows = [
            WindowFinding(
                id=mid,
                label=catalog[mid].label if mid in catalog else mid,
                provider=prov,
                catalog_window=ours,
                provider_window=theirs,
                builtin=is_builtin_model(mid),
            )
            for mid, ours, theirs in (
                [(mid, None, win) for mid, win in report.window_backfill]
                + [(mid, ours, theirs) for mid, ours, theirs in report.window_drift]
            )
        ]

        out.append(ModelSyncReport(
            provider=prov,
            offered=len(found),
            skipped=None,
            probed=probe,
            missing=report.missing,
            unreachable=unreachable,
            windows=windows,
            new_models=[
                DiscoveredModel(
                    id=m.id, label=m.label, provider=m.provider,
                    context_window=m.context_window, description=m.description,
                    likely_chat=m.likely_chat,
                )
                for m in report.new
            ],
        ))
    return out
