from __future__ import annotations

import asyncio
import ast
import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, Optional
from pathlib import Path
from agriconnect.core.get_llm import get_llm
from agriconnect.services.market.security import SecurityService

logger = logging.getLogger("Agent.MarketCoach")

class MarketRuntimeError(RuntimeError):
    """Erreur spécifique au runtime du MarketCoach."""
    pass



# --- UTILS ---

async def _maybe_await(obj: Any) -> Any:
    """Utilitaire pour gérer les appels synchrones ou asynchrones du runtime."""
    if hasattr(obj, "__await__") or asyncio.iscoroutine(obj):
        return await obj
    return obj

def _to_float(value: Any) -> Optional[float]:
    """Sécurise la conversion numérique pour les calculs et la DB."""
    try:
        if value is None: return None
        return float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        return None

def _safe_node(fn: Callable[..., Awaitable[Dict[str, Any]]], name: str) -> Callable[..., Awaitable[Dict[str, Any]]]:
    """Décorateur pour sécuriser les noeuds LangGraph."""
    async def _wrapped(state: Dict[str, Any], mc_runtime: "MarketRuntime", **_: Any) -> Dict[str, Any]:
        try:
            # Ne pas exiger la connexion MCP ici : certains noeuds (NLU, modération)
            # fonctionnent sans accès DB. Les appels DB utiliseront `call_db`
            # qui lèvera une erreur claire si la session MCP manque.
            return await fn(state, mc_runtime)
        except Exception as exc:
            logger.exception("[MarketCoach] node=%s failed: %s", name, exc)
            return {
                "status": "ERROR",
                "error_message": f"Erreur technique dans le noeud '{name}'.",
                "technical_details": str(exc),
            }
    return _wrapped


def ensure_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {"status": "error", "message": "No response"}

    # If MCP already gave us a dict, we may still need to unwrap raw_result.
    if isinstance(obj, dict):
        raw = (obj.get("data") or {}).get("raw_result") if isinstance(obj.get("data"), dict) else None
        if isinstance(raw, str) and raw.strip():
            parsed = ensure_dict(raw)
            # If the embedded payload is a dict-like response, prefer it.
            if isinstance(parsed, dict) and parsed is not obj:
                return parsed
        return obj

    if isinstance(obj, str):
        obj = obj.strip()
        try:
            # Tentative 1 : JSON Standard
            return json.loads(obj)
        except json.JSONDecodeError:
            try:
                # Tentative 2 : Nettoyage des guillemets simples (fréquent avec MCP)
                cleaned = obj.replace("'", '"')
                return json.loads(cleaned)
            except:
                try:
                    # Tentative 3 : Littéral Python (ex: "{'status': 'error', ...}")
                    parsed = ast.literal_eval(obj)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    pass

                # String brute non interprétable => traiter comme une erreur.
                logger.warning(f"Réponse brute non-JSON reçue du MCP : {obj[:50]}...")
                return {
                    "status": "error",
                    "data": {"raw_result": obj},
                    "message": "Non-JSON response from MCP"
                }
    
    # Si c'est un objet (ex: Result de MCP)
    if hasattr(obj, "content"): # Format spécifique à certains clients MCP
        return {"status": "ok", "data": obj.content}

    return {"status": "ok", "data": str(obj)}


def is_success_response(res: Dict[str, Any]) -> bool:
    status = str((res or {}).get("status") or "").lower()
    if status in {"ok", "success", "completed"}:
        return True
    if status in {"error", "failed", "rejected", "not_found"}:
        return False
    if (res or {}).get("error"):
        return False
    return "status" not in (res or {})


def norm_intent(intent: Any) -> str:
    return str(intent or "UNKNOWN").upper().split(".")[-1]


def extract_first_int(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\b(\d{1,3})\b", text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def merge_payload(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base or {})
    for k, v in (updates or {}).items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        merged[k] = v
    return merged


def friendly_missing(intent: str, missing: list[str], intent_config: Dict[str, Any]) -> list[str]:
    cfg = (intent_config or {}).get(intent, {})
    label_map = cfg.get("label_map", {}) if isinstance(cfg, dict) else {}
    return [label_map.get(field, field) for field in missing]


def build_confirmation_summary(intent: str, payload: Dict[str, Any], intent_config: Dict[str, Any]) -> str:
    cfg = (intent_config or {}).get(intent, {}) if isinstance(intent_config, dict) else {}
    label = cfg.get("label", intent)

    product = (payload or {}).get("product")
    qty = (payload or {}).get("quantity_mentioned")
    unit = (payload or {}).get("unit_mentioned")
    price = (payload or {}).get("price_mentioned")
    currency = (payload or {}).get("currency") or "FCFA"

    lines = [f"✅ {label}"]
    if product:
        lines.append(f"- Produit: {product}")
    if qty is not None:
        lines.append(f"- Quantité: {qty} {unit or ''}".strip())
    if price is not None:
        lines.append(f"- Prix: {price} {currency}")

    lines.append("\nSi c'est correct, répondez juste: OK")
    return "\n".join(lines)


# Backward compat: old private name
_ensure_dict = ensure_dict


class MarketRuntime:
    def __init__(self, llm_client: Any = None, server_path: str | None = None, mcp_session: Any = None):
        self.llm = llm_client or get_llm()

        # If orchestrator already provides a session/client, use it directly.
        self.db_client = mcp_session
        self._server_path = None if mcp_session is not None else (server_path or self._detect_server_path())

        self.security = SecurityService(self.llm)
        self.model_answer = "llama-3.3-70b-versatile"

    def _detect_server_path(self) -> str:
        """Localise automatiquement le db_server.py dans l'arborescence."""
        current_path = Path(__file__).resolve()
        for parent in current_path.parents:
            # On cherche la racine qui contient le package agriconnect
            potential_path = parent / "agriconnect" / "protocols" / "mcp" / "servers" / "db_server.py"
            if potential_path.exists():
                return str(potential_path)
        
        logger.warning("Auto-detection: db_server.py introuvable.")
        return None

    async def __aenter__(self) -> MarketRuntime:
        """Initialise la connexion MCP."""
        if self.db_client is not None:
            return self

        if not self._server_path:
            raise MarketRuntimeError("Impossible de démarrer : chemin du serveur MCP inconnu.")

        from agriconnect.infrastructure.mcp.client import AgriMCPClient

        self.db_client = AgriMCPClient(self._server_path)
        await self.db_client.__aenter__()
        
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.db_client:
            await self.db_client.__aexit__(exc_type, exc_val, exc_tb)

    async def call_db(self, tool_name: str, **kwargs) -> Any:
        """
        Point d'entrée unique et direct pour la base de données.
        Ex: await runtime.call_db("get_farms", producer_id="123")
        """
        if not self.db_client:
            raise MarketRuntimeError("Runtime non connecté. Utilisez 'async with'.")

        import unicodedata

        def _ascii_fold_str(text: str) -> str:
            if not text:
                return text
            text = text.translate(str.maketrans({"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "’": "'"}))
            normalized = unicodedata.normalize("NFKD", text)
            return normalized.encode("ascii", "ignore").decode("ascii")

        def _ascii_fold_value(value: Any) -> Any:
            if isinstance(value, str):
                return _ascii_fold_str(value)
            if isinstance(value, list):
                return [_ascii_fold_value(v) for v in value]
            if isinstance(value, tuple):
                return tuple(_ascii_fold_value(v) for v in value)
            if isinstance(value, dict):
                return {k: _ascii_fold_value(v) for k, v in value.items()}
            return value

        # Postel's Law: strip None values before forwarding to MCP client
        safe_kwargs = {k: v for k, v in kwargs.items() if v is not None}
        # AXE 4: ensure MCP never receives accented strings
        safe_kwargs = _ascii_fold_value(safe_kwargs)

        try:
            return await self.db_client.call_tool(tool_name, safe_kwargs)
        except Exception as exc:
            logger.error(
                "MCP_CALL_FAILURE | tool=%s | args=%s | error=%s",
                tool_name,
                json.dumps(list(safe_kwargs.keys())),
                str(exc),
            )
            raise

    def ensure_ready(self):
        """Vérifie si le runtime est prêt et connecté."""
        if self.db_client is None:
            raise MarketRuntimeError(
                "Le Runtime n'est pas initialisé. "
                "Veuillez fournir mcp_session ou utiliser le bloc 'async with mc_runtime:'."
            )
        
    @classmethod
    def from_auto_path(cls, llm_client: Any = None) -> MarketRuntime:
        """Gardé pour compatibilité, mais maintenant alias du constructeur par défaut."""
        return cls(llm_client=llm_client)

def build_runtime(llm_client: Any = None, server_path: str = None) -> MarketRuntime:
    """
    Factory pour instancier le MarketRuntime.
    
    Args:
        llm_client: Client LLM (ex: Groq, OpenAI). Si None, le runtime utilisera get_llm().
        server_path: Chemin vers db_server.py. Si None, le runtime tentera 
                     l'auto-détection via _detect_server_path().
    
    Returns:
        Une instance prête de MarketRuntime (nécessite ensuite 'async with' pour la connexion).
    """
    return MarketRuntime(llm_client=llm_client, server_path=server_path)


def build_runtime_from_session(llm_client: Any = None, mcp_session: Any = None) -> MarketRuntime:
    """Factory pour runtime basé sur une session MCP déjà ouverte."""
    return MarketRuntime(llm_client=llm_client, mcp_session=mcp_session)


__all__ = [
    "MarketRuntime",
    "MarketRuntimeError",
    "_maybe_await",
    "_safe_node",
    "ensure_dict",
    "is_success_response",
    "norm_intent",
    "extract_first_int",
    "merge_payload",
    "friendly_missing",
    "build_confirmation_summary",
    "build_runtime",
    "build_runtime_from_session",
]


import asyncio
import sys
import logging
# Importe ton helper de normalisation qui est déjà utilisé dans tes nodes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def test_mcp_call_db():
    print("🚀 Initialisation du MarketRuntime...")
    runtime = build_runtime(llm_client=None, server_path=None)
    
    try:
        async with runtime as live_runtime:
            print("🔗 Connexion MCP établie. Exécution de 'get_market_snapshot'...")
            raw_response = await live_runtime.call_db("get_market_snapshot")
            
            # Normalisation identique à celle de tes nœuds de production
            response = ensure_dict(raw_response)
            
            print("\n================ ANSWER FROM MCP SERVER ================")
            print(response)
            print("========================================================\n")
            
            if isinstance(response, dict) and response.get("status") == "success":
                print("✅ TEST REUSSI : Le serveur MCP répond parfaitement et le format est standardisé !")
            else:
                print("⚠️ Connexion OK, mais le statut retourné n'est pas 'success'.")
                
    except Exception as e:
        print(f"❌ Échec critique lors du test call_db : {e}", file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(test_mcp_call_db())