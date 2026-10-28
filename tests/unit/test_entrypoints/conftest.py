"""Owner context for legacy route-only tests; authentication has dedicated tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def legacy_vault_owner_context(request, monkeypatch):
    # Legacy route tests inject their own stores/adapters, with or without
    # lifespan. Provide a trusted owner at that composition seam. Authentication
    # itself is exercised by the dedicated gateway HTTP and browser tests.
    if request.path.name not in {
        "test_memo_vault_exports.py",
        "test_umif_end_to_end.py",
        "test_claude_import.py",
        "test_memo_vault_imports.py",
    }:
        return
    import importlib

    module = importlib.import_module("everos.entrypoints.memo_vault.app")
    app = module.app
    security = SimpleNamespace(check=lambda _: None)
    monkeypatch.setattr(module, "OwnerSecurity", lambda *args, **kwargs: security)

    monkeypatch.setattr(
        app.state,
        "owner_security",
        security,
        raising=False,
    )
