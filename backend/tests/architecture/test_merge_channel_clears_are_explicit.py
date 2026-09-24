"""Invariant : un champ supprimé d'un canal `merge_dict` reste supprimé.

`agents/reducers.py::merge_dict` interprète une clé ABSENTE du patch comme « ne rien
changer ». Retirer une clé d'un dict (`d.pop(k)` / `del d[k]`) puis renvoyer ce dict dans
un canal `merge_dict` ne supprime donc RIEN : l'ancienne valeur survit. Bug réel vécu
plusieurs fois (`ambiguous_groups` collant, `working_memory.active_goal` qui ressuscite un
goal rejeté — audit B1).

La seule façon d'effacer une clé est `DELETE` (`agents/reducers.py`) : `{k: DELETE}` ou
`clear_keys(...)`. Ce test scanne le code du moteur et échoue dès qu'une fonction
`pop`/`del` une clé d'un dict qu'elle renvoie ensuite (directement ou par `**spread`) sous
une clé de canal `merge_dict`.
"""
from __future__ import annotations

import ast
import typing
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from ladini.agents.reducers import merge_dict
from ladini.graphs.agents.market_coach.core.state import MarketAgentState

_ENGINE_ROOT = Path(__file__).resolve().parents[2] / "src" / "ladini" / "graphs" / "agents" / "market_coach"


def merge_dict_channels() -> Set[str]:
    hints = typing.get_type_hints(MarketAgentState, include_extras=True)
    channels = set()
    for name, ann in hints.items():
        for meta in getattr(ann, "__metadata__", ()) or ():
            if meta is merge_dict:
                channels.add(name)
    return channels


class _FunctionScan(ast.NodeVisitor):
    def __init__(self, channels: Set[str], popping_helpers: Set[str] = frozenset()) -> None:
        self.channels = channels
        self.popping_helpers = popping_helpers
        self.removed: Dict[str, List[int]] = {}
        self.fed: Dict[str, Tuple[str, int]] = {}
        self.returned: Set[str] = set()
        self.aliases: Dict[str, str] = {}  # copie -> source (`y = dict(x)`, `y = {**x}`)

    def visit_Return(self, node: ast.Return) -> None:
        if isinstance(node.value, ast.Name):
            self.returned.add(node.value.id)
        self.generic_visit(node)

    # `x.pop(...)` / `del x[...]`
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "pop" and isinstance(func.value, ast.Name):
            self.removed.setdefault(func.value.id, []).append(node.lineno)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                self.removed.setdefault(target.value.id, []).append(node.lineno)
        self.generic_visit(node)

    def _feed(self, key: str, value: ast.AST, lineno: int) -> None:
        if key not in self.channels:
            return
        if isinstance(value, ast.Name):
            self.fed.setdefault(value.id, (key, lineno))
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in self.popping_helpers
        ):  # "working_memory": _clear_goal_lock()  (helper qui renvoie un dict amputé)
            self.fed.setdefault(f"{value.func.id}()", (key, lineno))
            self.removed.setdefault(f"{value.func.id}()", []).append(lineno)
        elif isinstance(value, ast.Dict):
            has_reset = any(isinstance(k, ast.Constant) and k.value == "__reset__" for k in value.keys)
            if has_reset:
                return  # remplacement intégral explicite : les clés retirées disparaissent
            for k, v in zip(value.keys, value.values, strict=True):
                if k is None and isinstance(v, ast.Name):  # {**x, ...}
                    self.fed.setdefault(v.id, (key, lineno))

    # {"working_memory": x} / {"working_memory": {**x}}
    def visit_Dict(self, node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values, strict=True):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                self._feed(k.value, v, node.lineno)
        self.generic_visit(node)

    @staticmethod
    def _copied_name(value: ast.AST):
        if isinstance(value, ast.Name):
            return value.id
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in {"dict", "normalize_slot_keys"}
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Name)
        ):
            return value.args[0].id
        if isinstance(value, ast.Dict) and value.keys and all(k is None for k in value.keys):
            if len(value.values) == 1 and isinstance(value.values[0], ast.Name):
                return value.values[0].id
        return None

    # patch["working_memory"] = x   /   y = dict(x)
    def visit_Assign(self, node: ast.Assign) -> None:
        source = self._copied_name(node.value)
        for target in node.targets:
            if source and isinstance(target, ast.Name) and target.id != source:
                self.aliases[target.id] = source
        for target in node.targets:
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                self._feed(target.slice.value, node.value, node.lineno)
        self.generic_visit(node)

    # Fonction imbriquée : ses propres patchs sont scannés séparément, MAIS une closure
    # qui retire une clé d'un dict de la fonction englobante (ex. `_cascade_clear` qui
    # fait `payload.pop(...)`) ampute bien ce dict — on relève donc ses retraits.
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        inner = _FunctionScan(self.channels)
        for stmt in node.body:
            inner.visit(stmt)
        for name, lines in inner.removed.items():
            self.removed.setdefault(name, []).extend(lines)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]


def _functions(tree: ast.AST):
    return [f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _popping_helpers(tree: ast.AST, channels: Set[str]) -> Set[str]:
    """Fonctions du module qui renvoient un dict dont elles ont retiré une clé."""
    helpers = set()
    for func in _functions(tree):
        scan = _FunctionScan(channels)
        for stmt in func.body:
            scan.visit(stmt)
        if scan.returned & set(scan.removed):
            helpers.add(func.name)
    return helpers


def find_violations(root: Path = _ENGINE_ROOT) -> List[str]:
    channels = merge_dict_channels()
    violations: List[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        helpers = _popping_helpers(tree, channels)
        for func in _functions(tree):
            scan = _FunctionScan(channels, helpers)
            for stmt in func.body:
                scan.visit(stmt)
            for fed_name, (channel, fed_line) in scan.fed.items():
                chain, name = [fed_name], fed_name
                while name in scan.aliases and scan.aliases[name] not in chain:
                    name = scan.aliases[name]
                    chain.append(name)
                for name, removed_line in [(n, ln) for n in chain for ln in scan.removed.get(n, ())]:
                    rel = path.relative_to(root)
                    violations.append(
                        f"{rel}:{removed_line} {func.name}(): `{name}` perd une clé "
                        f"(pop/del) puis alimente le canal merge_dict `{channel}` (l.{fed_line})"
                    )
    return violations


def test_merge_dict_channels_are_discovered_from_the_state_contract():
    channels = merge_dict_channels()
    assert {"working_memory", "transaction_payload", "extracted_entities"} <= channels


def test_no_engine_code_clears_a_merge_channel_key_by_removing_it():
    violations = find_violations()
    assert violations == [], (
        "Une clé retirée d'un dict n'est PAS effacée par merge_dict — utiliser "
        "`DELETE`/`clear_keys` (ladini.agents.reducers) :\n  " + "\n  ".join(violations)
    )


# ---------------------------------------------------------------------
# Le garde-fou lui-même : il doit attraper chaque forme connue du bug.
# ---------------------------------------------------------------------

_BAD_SNIPPETS = {
    "direct_pop": """
def node(state):
    payload = dict(state.get("transaction_payload") or {})
    payload.pop("quantity", None)
    return {"transaction_payload": payload}
""",
    "del_then_spread": """
def node(state):
    wm = dict(state.get("working_memory") or {})
    del wm["active_goal"]
    return {"working_memory": {**wm}}
""",
    "subscript_assignment": """
def node(state):
    patch = {}
    payload = dict(state.get("transaction_payload") or {})
    payload.pop("price", None)
    patch["transaction_payload"] = payload
    return patch
""",
    "helper_returning_a_popped_dict": """
def node(state):
    working = state.get("working_memory") or {}
    def _clear():
        wm = dict(working)
        wm.pop("active_goal", None)
        return wm
    return {"working_memory": _clear()}
""",
    "closure_popping_the_outer_dict": """
def node(state):
    payload = dict(state.get("transaction_payload") or {})
    def _cascade():
        payload.pop("price", None)
    _cascade()
    return {"transaction_payload": payload}
""",
    "alias_copy": """
def node(state):
    stable = dict(state.get("stable_entities") or {})
    stable.pop("product", None)
    updated = dict(stable)
    return {"stable_entities": updated}
""",
}

_GOOD_SNIPPETS = {
    "delete_marker": """
def node(state):
    payload = dict(state.get("transaction_payload") or {})
    payload["quantity"] = DELETE
    return {"transaction_payload": payload}
""",
    "full_reset": """
def node(state):
    payload = dict(state.get("transaction_payload") or {})
    payload.pop("quantity", None)
    return {"transaction_payload": {"__reset__": True, **payload}}
""",
    "local_pop_never_returned": """
def node(state):
    args = dict(state.get("transaction_payload") or {})
    args.pop("internal", None)
    call(args)
    return {"status": "OK"}
""",
}


@pytest.mark.parametrize("name", sorted(_BAD_SNIPPETS))
def test_the_guard_catches_every_known_shape_of_the_bug(tmp_path, name):
    (tmp_path / "mod.py").write_text(_BAD_SNIPPETS[name])
    assert find_violations(tmp_path), name


@pytest.mark.parametrize("name", sorted(_GOOD_SNIPPETS))
def test_the_guard_accepts_explicit_clears(tmp_path, name):
    (tmp_path / "mod.py").write_text(_GOOD_SNIPPETS[name])
    assert find_violations(tmp_path) == [], name
