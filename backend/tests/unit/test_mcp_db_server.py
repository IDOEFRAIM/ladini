"""`protocols/mcp/servers/db_server.py::AgriConnectMCPEntryPoint` — audit
MCP/AGUI 2026-08-27, Tâche 4 : sépare explicitement les lectures pures
(Resources, adressables par URI ``agriconnect://catalog/{name}``) des
actions (Tools). Avant ce chantier, `_PUBLIC_CATALOG_TOOLS`
(`infrastructure/mcp/runtime.py`) existait déjà mais n'était raccordé à
aucun transport MCP — un host ne pouvait pas les découvrir sans invoquer un
Tool générique.

Ce module importe `db_server.py`, qui redirige stdout/stderr vers
`server_debug.log` UNIQUEMENT quand il est lancé comme process réel (voir
`if __name__ == "__main__"` dans le fichier) — un simple `import` ou une
instanciation de `AgriConnectMCPEntryPoint` ne touchent ni stdout/stderr ni
le système de fichiers. `test_importing_the_module_has_no_side_effects`
verrouille explicitement cette propriété : une régression ici casserait
silencieusement toute la suite de tests (stdout redirigé de façon
irréversible pour le reste du process pytest)."""
from __future__ import annotations

import json
import sys

import pytest

from tests.conftest import run


def _entry():
    import agriconnect.protocols.mcp.servers.db_server as mod
    return mod.AgriConnectMCPEntryPoint()


class TestImportHasNoSideEffects:
    def test_importing_the_module_has_no_side_effects(self):
        before_stdout = sys.stdout
        before_stderr = sys.stderr

        import agriconnect.protocols.mcp.servers.db_server as mod  # noqa: F401

        assert sys.stdout is before_stdout, "l'import a redirigé stdout"
        assert sys.stderr is before_stderr, "l'import a redirigé stderr"

    def test_constructing_the_entry_point_has_no_side_effects(self):
        before_stdout = sys.stdout
        before_stderr = sys.stderr

        _entry()

        assert sys.stdout is before_stdout, "la construction a redirigé stdout"
        assert sys.stderr is before_stderr, "la construction a redirigé stderr"


class TestListResources:
    def test_lists_every_public_catalog_tool_as_a_resource(self):
        from agriconnect.infrastructure.mcp.runtime import _PUBLIC_CATALOG_TOOLS

        entry = _entry()
        resources = run(entry._list_resources())

        uris = {str(r.uri) for r in resources}
        expected = {f"agriconnect://catalog/{name}" for name in _PUBLIC_CATALOG_TOOLS}
        assert uris == expected

    def test_each_resource_carries_a_name_and_a_description(self):
        entry = _entry()
        resources = run(entry._list_resources())

        assert resources, "au moins une resource publique attendue"
        for r in resources:
            assert r.name, f"nom manquant pour {r.uri}"
            assert r.mimeType == "application/json"
            # La description vient de list_tools_by_name() — jamais vide pour
            # un outil réellement déclaré dans TOOL_DESCRIPTIONS (h.py).
            assert isinstance(r.description, str)

    def test_does_not_list_a_write_tool_as_a_resource(self):
        """Non-régression : create_order (action, effet de bord) ne doit
        JAMAIS apparaître comme Resource — seul _PUBLIC_CATALOG_TOOLS
        (lecture pure, sans identité) est éligible."""
        entry = _entry()
        resources = run(entry._list_resources())
        names = {r.name for r in resources}
        assert "create_order" not in names


class TestReadResource:
    def test_reads_a_valid_catalog_resource_and_returns_json(self):
        entry = _entry()

        async def _fake_call_tool(name, arguments=None):
            assert name == "get_available_zones"
            assert arguments == {}
            return {"status": "success", "data": [{"id": "z1", "label": "Ouagadougou"}]}

        entry.backend.call_tool = _fake_call_tool

        raw = run(entry._read_resource("agriconnect://catalog/get_available_zones"))
        payload = json.loads(raw)
        assert payload == {"status": "success", "data": [{"id": "z1", "label": "Ouagadougou"}]}

    def test_serializes_non_ascii_content_without_escaping(self):
        """ensure_ascii=False — un nom de zone accentué ne doit pas
        ressortir en séquences \\uXXXX illisibles pour un client MCP."""
        entry = _entry()

        async def _fake_call_tool(name, arguments=None):
            return {"status": "success", "data": [{"label": "Bobo-Dioulasso — région Ouest"}]}

        entry.backend.call_tool = _fake_call_tool
        raw = run(entry._read_resource("agriconnect://catalog/get_available_zones"))
        assert "Bobo-Dioulasso" in raw
        assert "\\u" not in raw

    def test_rejects_a_uri_outside_the_public_catalog(self):
        """Une action (Tool avec effet de bord) ne doit jamais être
        atteignable via le chemin Resource, même en connaissant son URI."""
        entry = _entry()
        with pytest.raises(ValueError, match="Resource inconnue"):
            run(entry._read_resource("agriconnect://catalog/create_order"))

    def test_rejects_a_completely_unknown_uri(self):
        entry = _entry()
        with pytest.raises(ValueError, match="Resource inconnue"):
            run(entry._read_resource("agriconnect://catalog/totally_made_up"))

    def test_a_valid_uri_never_reaches_the_backend_when_rejected(self):
        """Le rejet doit avoir lieu AVANT tout appel backend — pas de fuite
        d'exécution pour un nom hors catalogue public."""
        entry = _entry()
        calls = []

        async def _spy_call_tool(name, arguments=None):
            calls.append(name)
            return {"ok": True}

        entry.backend.call_tool = _spy_call_tool
        with pytest.raises(ValueError):
            run(entry._read_resource("agriconnect://catalog/drop_table"))
        assert calls == [], "le backend n'aurait jamais dû être appelé"
