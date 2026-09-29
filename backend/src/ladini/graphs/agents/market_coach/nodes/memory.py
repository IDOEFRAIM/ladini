import unicodedata
from typing import Any, Dict, Optional

from ladini.core.logger import get_logger
from ladini.domain.commercial_offer_flow import (
    FIELD_PACKAGE_SIZE,
    FIELD_PRICE_BASIS,
    parse_basis_reply,
    parse_package_content,
    price_expression_in_text,
)
from ladini.graphs.agents.market_coach.core.goals import is_goal_refinement
from ladini.graphs.agents.market_coach.core.pending_interaction import (
    CART_TUNNEL_KINDS,
    DISAMBIGUATION_MENU_GOAL_SHIM,
    InteractionKind,
    get_pending_interaction,
    to_tunnel_category,
)
from ladini.graphs.agents.market_coach.core.slots import (
    SLOT_FILLING_INPUTS,
    build_alias_mirrors,
    fields_for_expected_input,
)
from ladini.graphs.agents.market_coach.core.state_compaction import (
    build_compaction_patch,
)
from ladini.graphs.agents.market_coach.nodes.cleaner import (
    _EPHEMERAL_WORKING_KEYS as _EPHEMERAL_WORKING_KEYS_TUPLE,
)
from ladini.graphs.agents.market_coach.services.domain.commercial_gate import (
    commercial_question_from_state,
)
from ladini.graphs.agents.market_coach.services.domain.slot_enrichment import (
    enrich_payload_from_text,
)
from ladini.graphs.agents.market_coach.services.menu_snapshot import (
    menu_snapshot_store,
    snapshot_belongs_to_active_menu,
)
from ladini.graphs.agents.market_coach.utils import (
    _CANONICAL_FIELD_ALIASES,
    MarketRuntime,
    _normalize_quantity_to_kg,
    canonical_unit_label,
    merge_payload,
    normalize_slot_keys,
    slot_has_value,
)

logger = get_logger("Ladini.MarketCoach.MemoryUpdate")

_ADD_TO_CART_DRAFT_FIELDS = {
    "product",
    "product_id",
    "quantity",
    "unit",
    "price",
    "zone",
}
# `selection_index`/`selected_value`/`resolved_id` (2026-08-30, refonte
# "palier avant quantité") : ce sont des signaux de sélection AG-UI
# TRANSITOIRES pour LE tour courant, jamais des données de "brouillon" à
# reconstituer plus tard. Incident réel : capturés ici dans `draft_payload`
# (un canal `merge_dict` séparé, jamais nettoyé entre-temps), ils
# survivaient au tour où le palier était choisi — puis `payload =
# merge_payload(draft_payload, payload)` plus bas (le brouillon comble les
# trous du payload courant) les RESSUSCITAIT sur un tour ultérieur dès que
# `payload["selection_index"]` valait `None` (un `None` ne "comble" pas un
# trou pour `merge_payload`, donc la valeur périmée du brouillon gagnait) —
# une réponse quantité se faisait alors ré-interpréter comme un ancien
# index de sélection.


# Analytics Phase C (2026-09-27) : "LITRE" manquait ici depuis son ajout au
# registre canonique le 2026-08-29 (`domain/quantity_unit.py::VALID_UNITS`)
# — une réponse "litre" à "quelle unité ?" était donc rejetée par
# `_resolve_unit_value` malgré un canonical_unit_label correct. Voir aussi le
# correctif jumeau dans `market_coach/utils.py::_CANONICAL_UNIT_MAP` (les
# alias "L"/"LITRES" ne se repliaient pas non plus sur "LITRE").
_PRIMARY_CANONICAL_UNITS = {"KG", "TONNE", "SAC", "UNITE", "PANIER", "TETE", "LITRE"}

# expected_input (slot précis en cours) → champs canoniques qu'une réponse
# ANSWER à ce slot a le droit de modifier. Tout le reste est du bruit/une
# hallucination LLM et doit être ignoré — voir le garde-fou dans `memory_update`.
#
# (2026-09-09, audit Bloc 2, Blocker D) : la base de chaque catégorie est
# désormais DÉRIVÉE de `core/slots.py::fields_for_expected_input` (source
# unique, réciproque de `expected_input_for_field` déjà utilisé partout
# ailleurs) plutôt que recopiée à la main — une copie manuelle avait dérivé
# du registre : `production_type` (PRODUCT) et `surface` (QUANTITY) sont
# bien mappés dans `core/slots.py` mais étaient ABSENTS de cette allowlist,
# donc une réponse dégradée au slot EXACTEMENT demandé ("c'est une culture" /
# "2 hectares") se faisait jeter par son propre garde-fou anti-hallucination.
# Seuls les champs COMPAGNONS qui ne sont pas eux-mêmes des "slots"
# interrogeables (identifiants techniques résolus, structure de tarifs,
# contrat d'action structuré) restent des extras à la main ci-dessous.
_EXPECTED_INPUT_EXTRA_FIELDS: Dict[str, frozenset] = {
    "PRODUCT": frozenset({"product_id"}),
    # `pricing_tiers` (2026-08-29) : un repli sur modèle dégradé (fréquent —
    # voir GROQ_RATE_LIMIT_FALLBACK) filtrait tout champ hors de cet
    # allowlist pour le slot en cours — un producteur répondant avec
    # plusieurs tarifs/conditionnements ("25 L à 500 FCFA et 40 L à
    # 900 FCFA") en réponse à la question quantité OU prix voyait
    # `pricing_tiers` silencieusement jeté, la confirmation retombant sur le
    # gabarit à tarif unique avec des valeurs incohérentes (quantité/prix
    # d'un tour précédent). `pricing_tiers` doit survivre sur CES DEUX slots :
    # c'est là qu'un producteur énumère naturellement ses déclinaisons.
    # `agent_action`/`action_*` (2026-09-01) : le contrat d'action structurée
    # (voir domain/selection_actions.py) répond au slot QUANTITY quand
    # SET_PACKAGE_COUNT/SET_QUANTITY est l'action attendue ou quand une
    # re-sélection de palier (SELECT_PRICING_TIER) survient pendant cette
    # même question — sans cette entrée, un repli sur modèle Groq dégradé
    # (429) aurait silencieusement jeté ces champs, rouvrant exactement
    # l'incident que ce contrat corrige.
    # `unit` (2026-09-09, régression réelle trouvée après déploiement du
    # Blocker D) : `core/slots.py::_EXPECTED_INPUT_MAP` classe le CHAMP
    # `unit` dans sa PROPRE catégorie ("UNIT"), distincte de "QUANTITY" —
    # `fields_for_expected_input("QUANTITY")` ne le renvoie donc jamais. Mais
    # une réponse à « quelle quantité ? » porte quasi-systématiquement les
    # DEUX ensemble ("600 L", "50 kg") — retirer `unit` d'ici (fait par
    # erreur en simplifiant la table vers la dérivation automatique) faisait
    # jeter l'unité de CHAQUE réponse quantité en repli dégradé (incident
    # réel : "j'ai 600 l" → `unit='LITRE'` journalé "hors-sujet ignoré").
    # Relation FIGÉE ici volontairement (impossible à dériver de
    # `_EXPECTED_INPUT_MAP`, qui répond à une question différente : "à quelle
    # catégorie appartient CE champ", pas "quels champs accompagnent
    # légitimement CETTE catégorie").
    "QUANTITY": frozenset(
        {
            "unit",
            "pricing_tiers",
            "agent_action", "action_offer_id", "action_producer_id", "action_pricing_tier_id",
            "action_package_count", "action_quantity", "action_unit",
        }
    ),
    "PRICE": frozenset({"price_unit", "pricing_tiers"}),
}
#: (Phase 2 hardening, commit 7) : la liste des CATÉGORIES elle-même était encore recopiée à la
#: main ici — une 3e copie du même ensemble que `core/slots.py::SLOT_FILLING_INPUTS` (déjà
#: dérivé de `_EXPECTED_INPUT_MAP.values()`), le exact "double dérive" que le commentaire
#: ci-dessus (2026-09-09) documentait pourtant déjà comme corrigé pour les CHAMPS. `WEEKLY_DAYS`
#: (ajouté au registre pour fermer le gap `weekly_days`) aurait dû être recopié ici À LA MAIN une
#: 3e fois sans cette dérivation — désormais impossible à oublier : toute catégorie ajoutée à
#: `_EXPECTED_INPUT_MAP` apparaît ici automatiquement.
_EXPECTED_INPUT_ALLOWED_FIELDS: Dict[str, frozenset] = {
    category: fields_for_expected_input(category) | _EXPECTED_INPUT_EXTRA_FIELDS.get(category, frozenset())
    for category in SLOT_FILLING_INPUTS
}

# _ALIAS_MIRRORS is now derived from core/slots.py (single source of truth).
_ALIAS_MIRRORS = build_alias_mirrors()

_CORRECTION_HISTORY_LIMIT = 5
_ORDER_MAPPING_KINDS = frozenset(
    {
        "order",
        "order_list",
        "buyer_orders",
        # (2026-09-13, incident WhatsApp #6) : les menus numérotés posés par
        # `flows/producer/flow.py::_resolve_order_for_confirmation`/
        # `_resolve_order_for_cancellation`/`_resolve_order_for_delivery_payment`
        # n'étaient PAS dans cet ensemble — une réponse "1"/"2" à leur menu
        # était bien classée `SELECTION` (fast-path numérique) mais son
        # `selection_index` était ensuite POPÉ ici SANS jamais écrire
        # `payload["order_id"]` (aucune branche ci-dessous ne matchait,
        # repli silencieux sur `payload["resolved_id"]`, jamais lu par ces
        # resolvers). Le tour suivant retrouvait donc un payload sans
        # `selection_index` NI `order_id` — le resolver, incapable de savoir
        # quelle commande avait été choisie, réaffichait indéfiniment le
        # même menu (boucle réelle observée en prod sur la confirmation
        # producteur, réponse "1" répétée sans effet).
        "order_confirmation",
        "order_cancellation",
        "order_delivery_payment",
    }
)
_AUCTION_MAPPING_KINDS = frozenset({"auction", "buyer_auction_list", "auction_bids"})
# SOURCE UNIQUE : `nodes/cleaner.py` (le nœud qui les remet à None en fin de
# tour). memory.py les EXCLUT de la mémoire de travail reprise au tour suivant —
# les deux doivent porter exactement le même ensemble, sinon une clé « nettoyée »
# d'un côté est réhydratée de l'autre (et inversement). Elles étaient
# auparavant déclarées deux fois à l'identique.
_EPHEMERAL_WORKING_KEYS = frozenset(_EPHEMERAL_WORKING_KEYS_TUPLE)
_PRODUCT_CASCADE_FIELDS = (
    "quantity",
    "unit",
    "price",
    "currency",
    "is_negotiable",
    "variety",
    "quality_grade",
    "quantity_display",
    "original_quantity",
    "unit_conversion",
    # Phase B1 : la base du prix, le conditionnement et l'offre commerciale sérialisée sont
    # ceux d'UN produit — un autre produit ne les hérite jamais (« lait au sachet de 0,5 L »
    # ne contamine pas « boeufs »).
    "unit_display",
    "original_unit",
    "price_unit",
    "price_conversion_note",
    "commercial_offer",
)
_UNIT_CASCADE_FIELDS = ("price", "price_unit", "commercial_offer")
_CANONICAL_SLOT_ORDER = ("product", "quantity", "unit", "price", "zone")


# (2026-09-09, audit Bloc 2, Blocker A) : `_is_goal_refinement`/la liste des
# spécialisations BUYER_REQUEST ont été relocalisées dans `core/goals.py`
# (source unique des RELATIONS entre goals — tunnels, breakout, et
# maintenant refinement) — ce nœud en reste le seul appelant, mais la
# RELATION elle-même est une propriété du catalogue de goals, pas de ce
# nœud. Voir `core/goals.py::is_goal_refinement` pour le contrat complet.


def _mirror_aliases(container: Dict[str, Any]) -> None:
    for canonical, aliases in _ALIAS_MIRRORS.items():
        value = container.get(canonical)
        for alias in aliases:
            if slot_has_value(value):
                container[alias] = value
            elif alias in container:
                # `pop` laissait l'ancien alias vivant dans le canal merge_dict.
                container[alias] = None


def _null_stale_aliases(payload: Dict[str, Any], source: Dict[str, Any]) -> None:
    """Remet à None les ALIAS encore vivants dans le canal quand leur clé canonique est vide.

    `payload` est `normalize_slot_keys(source)` : les alias (`quantite`, `qty`, `prix`,
    `montant`…) y sont déjà repliés sur le canonique puis ABSENTS du patch renvoyé — le canal
    `merge_dict` les garde donc à leur ancienne valeur. Tant que le canonique est renseigné le
    miroir les réécrit (`_mirror_aliases`) ; mais dès qu'une cascade l'a vidé (changement de
    produit, d'unité), `normalize_slot_keys` du validateur RESSUSCITAIT l'ancienne quantité/prix
    depuis l'alias : « finalement je vends des boeufs » gardait « 50 » et « 500 FCFA » du lait."""
    for raw_key, value in source.items():
        if not isinstance(raw_key, str) or not slot_has_value(value):
            continue
        canonical = _CANONICAL_FIELD_ALIASES.get(raw_key.lower())
        if canonical and canonical != raw_key and not slot_has_value(payload.get(canonical)):
            payload[raw_key] = None


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _format_value(value: Any) -> str:
    if not slot_has_value(value):
        return "∅"
    if isinstance(value, float):
        return "%g" % value
    return str(value)


def _resolve_unit_value(value: Any) -> Optional[str]:
    if not slot_has_value(value):
        return None
    raw = str(value).strip().upper()
    if not raw:
        return None
    canonical = canonical_unit_label(raw, raw)
    canonical = str(canonical or "").upper().strip()
    if canonical in _PRIMARY_CANONICAL_UNITS:
        return canonical
    return None


#: Kinds d'interaction qui signifient « une sélection est réellement
#: attendue ce tour-ci » — seule situation où le filet de secours
#: `menu_snapshot_store` a le droit de résoudre une réponse numérique
#: (micro-passe finale 2026-09-09, Sujet A). Voir
#: `services/menu_snapshot.py::snapshot_belongs_to_active_menu` pour la
#: justification de l'ensemble exact.
_SELECTION_PENDING_KINDS = frozenset({InteractionKind.SELECTION_MENU}) | CART_TUNNEL_KINDS


def _selection_is_pending(state: Dict[str, Any]) -> bool:
    return get_pending_interaction(state).kind in _SELECTION_PENDING_KINDS


async def memory_update(
    state: Dict[str, Any], mc_runtime: MarketRuntime
) -> Dict[str, Any]:
    """Met à jour la mémoire de transaction, résout les sélections AG-UI,
    hérite les entités stables pour continuité inter-tours, et injecte
    les défauts profil (zone) quand l'utilisateur ne les spécifie pas."""
    extracted_raw: Dict[str, Any] = state.get("extracted_entities") or {}
    payload_source: Dict[str, Any] = dict(state.get("transaction_payload") or {})
    stable_source: Dict[str, Any] = dict(state.get("stable_entities") or {})
    working_source: Dict[str, Any] = dict(state.get("working_memory") or {})
    working: Dict[str, Any] = {
        key: value
        for key, value in working_source.items()
        if key not in _EPHEMERAL_WORKING_KEYS
    }
    draft_source: Dict[str, Any] = dict(state.get("draft_payload") or {})

    extracted = normalize_slot_keys(extracted_raw)
    payload = normalize_slot_keys(payload_source)

    # ── Phase B1 : une réponse à une QUESTION COMMERCIALE n'est jamais une nouvelle quantité ──
    # « 0,5 litre » (contenu d'un sachet) ou « par tonne » (base du prix) est interprété par le
    # `validator` depuis le TEXTE, dans le contexte de la question posée. Toute extraction
    # quantité/unité/prix/produit du classifieur sur ce tour est donc du bruit (le LLM lit
    # « 0,5 litre » comme quantity=0.5) et ne doit JAMAIS écraser l'offre établie.
    _commercial_q = commercial_question_from_state(state)
    _reply_text = state.get("normalized_text") or state.get("user_query") or ""
    _commercial_reply_turn = _commercial_q is not None and (
        (
            _commercial_q.requested_field == FIELD_PACKAGE_SIZE
            and parse_package_content(_reply_text, question=_commercial_q) is not None
        )
        or (
            _commercial_q.requested_field == FIELD_PRICE_BASIS
            and parse_basis_reply(_reply_text, commercial_unit=_commercial_q.expected_basis_unit)
            is not None
        )
    )
    if _commercial_reply_turn:
        for _noise in ("quantity", "unit", "price", "price_unit", "pricing_tiers", "product"):
            extracted.pop(_noise, None)
    elif str(state.get("current_goal") or "").upper() == "SALES_PUBLISH_PRODUCT":
        # Correction du contenu en cours de confirmation (« finalement 1L le sachet ») : le LLM lit
        # « 1L » comme une nouvelle quantité. Si le TEXTE est une phrase de contenu de
        # conditionnement ET que la « quantité » extraite n'est que ce contenu, on la retire.
        _content = parse_package_content(state.get("normalized_text") or state.get("user_query"))
        _established_qty = payload.get("quantity")
        if (
            _content is not None
            and slot_has_value(_established_qty)
            and slot_has_value(extracted.get("quantity"))
            and abs(float(extracted["quantity"]) - float(_content[0])) < 1e-9
            and abs(float(extracted["quantity"]) - float(_established_qty)) > 1e-9
        ):
            extracted.pop("quantity", None)
            extracted.pop("unit", None)
    # « finalement 600 le sachet » : « <nombre> le/par <unité> » est un PRIX. Le classifieur en fait
    # parfois une quantité/unité (600 SAC) qui, via `_apply_slot("unit")`, purgerait l'offre établie
    # (conditionnement compris) — on retire cette lecture avant toute fusion.
    _price_expr = (
        price_expression_in_text(state.get("normalized_text") or state.get("user_query"))
        if str(state.get("current_goal") or "").upper() == "SALES_PUBLISH_PRODUCT"
        else None
    )
    if _price_expr is not None and slot_has_value(payload.get("quantity")):
        _expr_amount, _expr_unit = _price_expr
        if slot_has_value(extracted.get("quantity")) and abs(float(extracted["quantity"]) - _expr_amount) < 1e-9:
            extracted.pop("quantity", None)
        _extracted_unit = extracted.get("unit")
        if slot_has_value(_extracted_unit) and canonical_unit_label(_extracted_unit, "") == _expr_unit:
            extracted.pop("unit", None)
    stable = normalize_slot_keys(stable_source)
    draft_payload = normalize_slot_keys(draft_source)

    # Valeurs déjà établies AVANT ce tour — capturées ici, avant toute fusion,
    # pour un garde-fou ciblé plus bas (voir `_established_quantity`/
    # `_established_price`) : un tour répondant au slot PRICE avec des
    # `pricing_tiers` ne doit jamais laisser le `quantity` redondant que le
    # LLM émet quand même (schéma JSON oblige) écraser une quantité DÉJÀ
    # donnée à un tour précédent — et symétriquement pour QUANTITY/price.
    _established_quantity = payload.get("quantity")
    _established_unit = payload.get("unit")
    _established_price = payload.get("price")

    # --- STALE TRANSIENT KEYS CLEANUP ---
    # transaction_payload uses merge_dict reducer → post_response_cleanup
    # cannot delete keys. Clear stale selection/resolved values here at the
    # start of every turn unless the fresh extracted_entities carries them.
    _interpreted_event_upper = str(state.get("interpreted_event") or "").upper().strip()
    if _interpreted_event_upper != "SELECTION":
        for _transient in ("selection_index", "selected_value", "resolved_id"):
            if _transient not in extracted:
                payload[_transient] = None  # `pop` ne nettoyait rien (merge_dict)

    # Bug réel production (2026-09-24, lifecycle de clarification `ambiguous_groups`) : même
    # classe de fuite que ci-dessus, sur deux clés-LISTE du micro-prompt NEW_TASK
    # (`ambiguous_groups`/`additional_items`, `interpreter/new_task_contract.py`). Le filtre
    # générique `slot_has_value` juste plus bas (ligne ~680, "for key, value in extracted.items():
    # if not slot_has_value(value): continue") ne COPIE jamais une liste vide dans `payload` —
    # correct pour ne pas écraser une vraie valeur par du bruit, mais ça laisse aussi
    # `payload["ambiguous_groups"]` d'UN tour précédent survivre indéfiniment dès que le tour
    # suivant n'a plus rien à y mettre (`extracted_entities` est le MÊME canal `merge_dict`, donc
    # même sans valeur cette clé n'est jamais "absente" une fois écrite une fois — voir
    # `interpreter/new_task_micro.py::_finalize`, qui l'inclut désormais toujours, même `[]`,
    # justement pour rendre CE nettoyage possible). Résultat observé sans ce nettoyage : après
    # "14 coqs et 57 moutons chèvres chaque semaine", TOUT message suivant sans nouvelle
    # ambiguïté rejouait la MÊME clarification, indéfiniment.
    # `payload.pop(...)` (comme au-dessus pour selection_index/...) ne suffit PAS ici : `merge_dict`
    # (`agents/reducers.py`) construit son résultat en partant de l'ANCIENNE valeur du canal et
    # n'écrase QUE les clés PRÉSENTES dans le nouveau dict retourné — une clé simplement ABSENTE
    # (parce que "poppée" localement) reste donc telle quelle dans l'ancienne valeur, jamais
    # effacée. Il faut l'assigner explicitement à `None` (une clé PRÉSENTE avec cette valeur EST
    # bien prise en compte par `merge_dict`) pour qu'elle disparaisse réellement du payload fusionné.
    # `orphan_quantities` (2026-09-26, `new_task_contract.py::NewTaskOrphanQuantity`) partage
    # exactement la même forme (liste, même schéma NEW_TASK) et le même risque de collage —
    # sans cette entrée, "150 kg tomate et 200 kg chaque semaine" (orphelin détecté) suivi de
    # "je veux 30 poulets chaque semaine" (tâche neuve, sans aucun orphelin) rejouait la
    # clarification "à quel produit correspondent les 200 KG ?" sur le NOUVEAU draft poulet.
    for _list_field in ("ambiguous_groups", "additional_items", "orphan_quantities", "correction_scope"):
        if not extracted.get(_list_field):
            payload[_list_field] = None
    onboarding_profile = dict(state.get("onboarding_profile") or {})

    def _rehydrate_onboarding_slot(
        source_key: str, target_key: Optional[str] = None
    ) -> None:
        key = target_key or source_key
        value = onboarding_profile.get(source_key)
        if slot_has_value(value) and not slot_has_value(payload.get(key)):
            payload[key] = value

    _rehydrate_onboarding_slot("phone")
    _rehydrate_onboarding_slot("name")
    _rehydrate_onboarding_slot("role")
    _rehydrate_onboarding_slot("zone_name")
    _rehydrate_onboarding_slot("zone_id")

    existing_corrections = working_source.get("recent_corrections") or {}
    if isinstance(existing_corrections, dict):
        recent_corrections = dict(existing_corrections)
    else:
        # Legacy format (list of dicts) → flatten
        recent_corrections = {}
        for entry in existing_corrections:
            if isinstance(entry, dict):
                for key, value in entry.items():
                    recent_corrections[key] = value
    payload_reset = False
    product_slot_changed = False
    clear_vendor_ctx = False

    def _record_correction(field: str, old: Any, new: Any) -> None:
        if not field:
            return
        change_value = f"{_format_value(old)} -> {_format_value(new)}"
        if field in recent_corrections:
            recent_corrections.pop(field, None)
        elif len(recent_corrections) >= _CORRECTION_HISTORY_LIMIT:
            first_key = next(iter(recent_corrections))
            recent_corrections.pop(first_key, None)
        recent_corrections[field] = change_value

    def _reset_payload(reason: str) -> None:
        nonlocal payload_reset, payload, clear_vendor_ctx
        if payload:
            logger.info("[MemoryUpdate] Payload reset (%s)", reason)
        payload.clear()
        payload_reset = True
        clear_vendor_ctx = True

    def _cascade_clear(fields) -> None:
        for field in fields:
            payload[field] = None  # `pop` ne nettoyait rien (merge_dict)

    def _clean_upper(value: Any) -> Optional[str]:
        if not slot_has_value(value):
            return None
        trimmed = str(value).strip().upper()
        return trimmed or None

    def _selection_tunnel_active() -> bool:
        """True quand un menu de sélection vendeur/palier est actif — voir
        `_apply_slot`, branche `field == "product"`. Incident réel
        (2026-08-30) : rien n'empêchait une extraction "product" parasite
        (ex: mauvais classement LLM en marge du tunnel) de purger le
        brouillon de panier en cours (`clear_vendor_ctx`, `stable_entities`)
        pendant qu'un menu vendeur/palier attend une réponse — perdant tout
        le contexte déjà résolu pour rien. Le fix #2 côté interpréteur
        (routing.py, verrouillage FSM) ferme le chemin qui produisait ce
        genre d'extraction parasite ; ce garde reste une défense en
        profondeur pour tout autre chemin d'extraction non tracé."""
        if str(working_source.get("available_mapping_kind") or "") in (
            "pricing_tier",
            "product_vendor",
        ):
            return True
        for ctx_key in ("tier_selection_context", "vendor_selection_context"):
            ctx = state.get(ctx_key)
            if isinstance(ctx, dict) and ctx and not ctx.get("__reset__"):
                return True
        return False

    def _apply_slot(field: str, value: Any) -> None:
        nonlocal product_slot_changed, clear_vendor_ctx
        if not slot_has_value(value):
            return
        # (Phase 2 hardening, commit 8, P1 audit 2026-09-24) : SEUL le chemin `expected_input
        # == "UNIT"` (réponse explicite à "quelle unité ?", voir `_resolve_unit_value` plus
        # haut) canonicalisait l'unité — une extraction NEW_TASK/UPDATE ordinaire ("j'ai 14
        # têtes de coqs") écrivait la valeur BRUTE du LLM directement dans `payload["unit"]`.
        # Un seul point de canonicalisation, ici, pour TOUTE écriture du slot `unit` — jamais
        # un second normalisateur par call site qui pourrait diverger (voir aussi
        # `flows/buyer/recurring_need.py::_clean_additional_items`, même correctif miroir pour
        # les items additionnels).
        if field == "unit" and isinstance(value, str):
            value = canonical_unit_label(value, value)
        current_value = payload.get(field)
        if not slot_has_value(current_value):
            payload[field] = value
            if field == "unit":
                # Une vraie extraction efface le drapeau posé par le défaut
                # non-confirmé de `validation.py::_apply_slot_defaults` —
                # voir `unit_was_assumed`.
                payload["unit_was_assumed"] = None
            return
        if _values_equal(current_value, value):
            return
        if field == "product" and _selection_tunnel_active():
            logger.info(
                "[MemoryUpdate] Ignored conflicting product slot '%s' -> '%s' "
                "while a selection tunnel is active — not purging vendor/tier context",
                current_value,
                value,
            )
            return
        _record_correction(field, current_value, value)
        if field == "product":
            _cascade_clear(_PRODUCT_CASCADE_FIELDS)
            stable["product"] = None
            stable["quantity"] = None
            stable["price"] = None
            stable["unit"] = None
            stable["stock_id"] = None
            product_slot_changed = True
            clear_vendor_ctx = True
        elif field == "unit":
            _cascade_clear(_UNIT_CASCADE_FIELDS)
            stable["unit"] = None
            payload["unit_was_assumed"] = None
        payload[field] = value

    def _normalize_menu_text(value: str | None) -> str:
        if not value:
            return ""
        normalized = unicodedata.normalize("NFKD", value)
        normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
        normalized = normalized.lower()
        return "".join(ch for ch in normalized if ch.isalnum())

    interpreted_event = str(state.get("interpreted_event") or "").upper().strip()
    # (2026-09-02, "no legacy shim") : source unique, dérivée de
    # `pending_interaction`.
    expected_input = to_tunnel_category(get_pending_interaction(state))
    form_step_state = str(state.get("form_step") or "").upper().strip()
    form_completed = form_step_state == "COMPLETE"
    onboarding_active = bool(
        state.get("is_onboarding")
        or interpreted_event == "ONBOARDING_INPUT"
        or str(state.get("detected_intent") or "").upper().strip() == "ONBOARDING"
        or str(state.get("response_strategy") or "").upper().strip() == "ONBOARDING"
    )

    if expected_input == "CANCELLATION_REASON":
        reason_text = state.get("normalized_text") or state.get("user_query")
        if isinstance(reason_text, str):
            reason_text = reason_text.strip()
            if reason_text:
                payload["cancel_reason"] = reason_text

    if expected_input == "UNIT":
        unit_candidates = [
            extracted.get("unit"),
            payload.get("unit"),
            stable.get("unit"),
            state.get("normalized_text"),
            state.get("user_query"),
        ]
        resolved_unit = None
        for candidate in unit_candidates:
            resolved_unit = _resolve_unit_value(candidate)
            if resolved_unit:
                break
        if resolved_unit:
            payload["unit"] = resolved_unit
            extracted["unit"] = resolved_unit
            # Avoid treating the unit answer as a product change.
            extracted.pop("product", None)

    incoming_role = _clean_upper(extracted.get("role"))
    if not incoming_role and not onboarding_active:
        incoming_role = _clean_upper(state.get("user_role"))
    if incoming_role == "UNKNOWN":
        incoming_role = None
    previous_role = _clean_upper(payload.get("role"))
    if (
        previous_role
        and incoming_role
        and previous_role != incoming_role
        and not onboarding_active
    ):
        _record_correction("role", previous_role, incoming_role)
        _reset_payload("role_change")
    elif (
        previous_role
        and incoming_role
        and previous_role != incoming_role
        and onboarding_active
    ):
        _record_correction("role", previous_role, incoming_role)
    if incoming_role:
        payload["role"] = incoming_role
    extracted.pop("role", None)

    detected_intent = (
        state.get("current_goal")
        or state.get("detected_intent")
        or extracted.get("intent")
    )
    incoming_intent = _clean_upper(detected_intent)
    if incoming_intent == "UNKNOWN":
        incoming_intent = None
    previous_intent = _clean_upper(payload.get("intent"))
    if (
        previous_intent
        and incoming_intent
        and previous_intent != incoming_intent
        and not onboarding_active
    ):
        if not is_goal_refinement(previous_intent, incoming_intent):
            _record_correction("intent", previous_intent, incoming_intent)
            if not form_completed:
                _reset_payload("intent_change")
    if incoming_intent and not onboarding_active:
        payload["intent"] = incoming_intent
    extracted.pop("intent", None)

    # FAILLE 1 — Sanctuarise the active business goal.
    # Never overwrite an active tunnel goal with None/UNKNOWN because the user replied briefly ("1", "ok", "oui").
    incoming_goal = state.get("current_goal")
    previous_goal = working.get("active_goal")
    in_tunnel = bool(previous_goal and expected_input not in {"", "NONE"})
    if (
        previous_goal
        and (
            incoming_goal in (None, "", "UNKNOWN")
            or interpreted_event in {"UNKNOWN", "ANSWER"}
        )
        and (
            in_tunnel
            or str(state.get("status") or "").upper()
            in {"WAITING_INPUT", "WAITING_CONFIRMATION"}
        )
    ):
        current_goal = previous_goal
    else:
        current_goal = incoming_goal

    goal_upper = str(current_goal or "").upper()

    if (
        goal_upper == "BUYER_ADD_TO_CART"
        and draft_payload
        and not draft_payload.get("__reset__")
    ):
        # `draft_payload` is in `_DRAFT_SAFE_GOALS` (state_cleaner_node), so it
        # is NEVER purged between two separate BUYER_ADD_TO_CART attempts as
        # long as the goal name doesn't change — only on FAILED/COMPLETED
        # status. If the user abandons adding product A mid-flow (no
        # FAILED/COMPLETED transition) and later starts adding product B, the
        # stale draft for A would otherwise resurrect onto B's payload. Guard:
        # only inherit the persisted draft if it's for the SAME product this
        # turn is extracting (i.e. a genuine continuation of the same
        # in-progress add), not a fresh add for a different one.
        incoming_product = extracted.get("product")
        draft_product = draft_payload.get("product")
        if (
            incoming_product
            and draft_product
            and not _values_equal(draft_product, incoming_product)
        ):
            logger.info(
                "[MemoryUpdate] Stale cart draft for '%s' discarded (new product '%s')",
                draft_product,
                incoming_product,
            )
        else:
            payload = merge_payload(draft_payload, payload)

    # Un tour CONFIRM ne fournit AUCUNE information nouvelle par définition
    # ("je confirme", "oui") — toute entité que l'interpréteur a pu extraire
    # pour un message aussi court/ambigu est du bruit (voire une
    # hallucination du LLM sur un few-shot du prompt système), jamais une
    # correction volontaire. Sans cette garde, `_apply_slot("product", ...)`
    # traitait un tel bruit comme un changement de produit légitime : il
    # écrasait le `product` que `form_node` venait JUSTE de figer dans
    # `transaction_payload` au moment de la confirmation, ET videait
    # quantity/unit/price/zone au passage (cascade de `_apply_slot` sur
    # changement de produit) — cause du bug où la commande exécutée portait
    # un tout autre produit que celui confirmé dans le récap.
    #
    # REJECT est différent : "non" seul ne porte en effet aucune info, mais
    # "non c'est 200 tonnes" / "non je vends X au prix de Y" est un refus AVEC
    # correction explicite — un pattern conversationnel très courant. Bloquer
    # inconditionnellement REJECT ici faisait perdre silencieusement toute
    # correction accompagnant un refus (le récap restait identique malgré une
    # reformulation limpide de l'utilisateur). On ne bloque donc REJECT que
    # si l'interpréteur n'a RÉELLEMENT rien extrait (cas du "non" bref) —
    # sinon les valeurs explicitement redonnées sont appliquées normalement.
    _extracted_has_real_values = any(slot_has_value(v) for v in extracted.values())
    # (2026-09-03, refonte transactionnelle) : une fois qu'un
    # `procurement_draft` canonique existe pour ce goal (voir
    # nodes/confirmation_gate.py + domain/procurement_draft.py), CE nœud
    # cesse d'être l'autorité de mutation de ses champs métier —
    # `flows/buyer/procurement_confirmation.py` lit `extracted_entities`
    # directement et transitionne le draft lui-même. Sans cette garde,
    # `transaction_payload` continuerait d'accumuler des mutations en
    # parallèle du draft (2 représentations concurrentes du même fait
    # métier, exactement l'anti-pattern qui a produit l'incident
    # `quantity_display` figé — mandat §22 : « pourquoi un état métier
    # transactionnel mutable est-il conservé dans un dict fusionné dont la
    # sémantique peut figer des valeurs ? »). `transaction_payload` reste
    # néanmoins écrit UNE fois, au tour de CONFIRM, par
    # `procurement_confirmation.py` (`draft.execution_payload()`) — jamais
    # par ce nœud, pour ce goal, à partir de ce point.
    _draft_owns_this_goal = (
        str(current_goal or "").upper() == "PROCUREMENT_CREATE_REQUEST"
        and slot_has_value(state.get("procurement_draft"))
    )
    _skip_merge = (
        interpreted_event == "CONFIRM"
        or (interpreted_event == "REJECT" and not _extracted_has_real_values)
        or _draft_owns_this_goal
    )

    # Garde-fou anti-hallucination pendant la collecte d'un slot PRÉCIS
    # (expected_input ∈ {DATE, PRICE, QUANTITY, UNIT, LOCATION, PRODUCT,
    # FARM_NAME}) : une réponse OU une correction visant ce slot ne doit jamais
    # pouvoir réécrire silencieusement des champs SANS RAPPORT déjà validés
    # (product/quantity/price...). Un LLM en repli dégradé (ex: llama-3.1-8b
    # après un 429 sur le modèle principal) hallucine des valeurs plausibles
    # pour TOUT le schéma JSON même quand une seule info a été donnée — observé
    # en prod DEUX fois :
    #   1) réponse à la DATE ("d'ici le 21 décembre") → "78 boeufs à 425000"
    #      remplacé par "5900 poussins allemand à 2400" ;
    #   2) correction de PRIX ("l'unité coûte plutôt 36500") pendant la DATE →
    #      "14 chèvres" remplacé par "36500 poussins allemands".
    # Le cas (2) passait par `event=UPDATE`, NON couvert par la 1re version de
    # ce garde (ANSWER seul) — d'où l'extension à UPDATE ici.
    #
    # IMPORTANT — pourquoi CONFIRMATION reste libre : `_EXPECTED_INPUT_ALLOWED_
    # FIELDS` n'a PAS de clé "CONFIRMATION"/"SELECTION"/"NONE" → `allowed_for_
    # slot` vaut None → ce bloc est un no-op. Une correction au moment de la
    # confirmation ("non c'est 200 tonnes", récap complet sous les yeux) peut
    # donc toujours toucher n'importe quel champ, comme voulu. Le narrowing ne
    # mord QUE pendant la collecte d'un slot isolé. INTERRUPTION est
    # volontairement exclu : il porte les entités d'un NOUVEAU goal, pas une
    # correction du goal courant.
    #
    # Même principe que le narrowing déjà appliqué par `agents/forms.py` et
    # `nodes/form_node.py` pour les formulaires DRY — ici pour le chemin
    # générique (validator/context_resolver) qui n'en bénéficiait pas.
    # Extraction composée du fast-path déterministe (routing.py) : un message
    # comme "775 kg d'oignon et le kg coûte 175 fcfa" répond au slot QUANTITY
    # en cours ET fournit price/price_unit — par correspondance exacte de
    # tokens (unité/devise), pas une supposition du LLM. Contrairement au LLM
    # en repli dégradé (source du garde-fou ci-dessous), cette extraction ne
    # peut PAS halluciner un champ sans rapport : elle ne remplit jamais que
    # quantity/unit/price/price_unit, les 4 champs qu'elle sait reconnaître
    # explicitement dans le texte. Sans cette exception, price/price_unit
    # étaient silencieusement rejetés comme "hors-scope" pendant la collecte
    # de QUANTITY — l'agent redemandait alors le prix au tour suivant alors
    # que l'utilisateur venait de le donner.
    _trusted_compound_path = (
        str((state.get("raw_analysis") or {}).get("path") or "")
        == "fast_path_slot_numeric_compound_answer"
    )

    # Le garde-fou ci-dessous ne visait QUE le modèle Groq de repli dégradé
    # (llama-3.1-8b après un 429 sur le modèle principal, voir le récit des 2
    # incidents ci-dessus) — jamais le modèle principal, qui suit déjà
    # l'instruction du prompt d'extraire PLUSIEURS champs à la fois quand le
    # message les donne ensemble ("500 tonnes, prix 300 FCFA/kg" → quantity ET
    # price). Avant ce fix, il s'appliquait à TOUTE réponse ANSWER/UPDATE, quel
    # que soit le modèle — un utilisateur qui répondait au tout premier slot
    # (PRODUCT, jamais couvert par le carve-out fast-path) avec plusieurs
    # infos à la fois ("tomates, 500kg à 200fcfa/kg") se faisait amputer de
    # tout sauf `product`, et l'agent redemandait la quantité/le prix au tour
    # suivant — l'agent restait "linéaire" malgré une extraction LLM correcte
    # en amont (bug remonté par l'utilisateur). `raw_analysis.degraded_model`
    # (interpreter/routing.py, lu depuis `completion.model` de la réponse
    # Groq) est le seul signal fiable pour savoir APRÈS COUP si le repli a été
    # utilisé pour CE tour précis.
    _degraded_model_response = bool(
        (state.get("raw_analysis") or {}).get("degraded_model")
    )

    if (
        interpreted_event in {"ANSWER", "UPDATE"}
        and not _skip_merge
        and not _trusted_compound_path
        and _degraded_model_response
    ):
        allowed_for_slot = _EXPECTED_INPUT_ALLOWED_FIELDS.get(expected_input)
        if allowed_for_slot is not None:
            for key in list(extracted.keys()):
                if key in allowed_for_slot:
                    continue
                dropped = extracted.pop(key, None)
                if slot_has_value(dropped):
                    logger.warning(
                        "[MemoryUpdate] %s attendu=%s : champ hors-sujet '%s'=%r ignoré "
                        "(hors-scope du slot en cours — probable bruit/hallucination LLM)",
                        interpreted_event,
                        expected_input,
                        key,
                        dropped,
                    )

    if not _skip_merge:
        for field in _CANONICAL_SLOT_ORDER:
            value = extracted.pop(field, None)
            _apply_slot(field, value)

        for key, value in list(extracted.items()):
            if not slot_has_value(value):
                continue
            payload[key] = value

    # --- ENTITY INHERITANCE : stable_entities → payload pour continuité ---
    # Pendant un tunnel, si le payload manque un champ stable, on l'injecte.
    # Cela évite de redemander le produit si l'utilisateur l'a déjà dit.
    if current_goal:
        skip_product_inheritance = payload_reset or product_slot_changed
        for key in ("product", "unit", "zone"):
            if slot_has_value(payload.get(key)) or not slot_has_value(stable.get(key)):
                continue
            if key == "product" and skip_product_inheritance:
                continue
            payload[key] = stable[key]
            logger.debug(
                "[MemoryUpdate] Inherited stable entity %s=%s", key, stable[key]
            )

    # --- VENDOR CONTEXT RECOVERY for cart tunnel ---
    # When the buyer is selecting a vendor, the product and quantity live in
    # vendor_selection_context but may be absent from payload (reset between
    # turns). Recover them so the validator doesn't block with "product missing".
    if goal_upper in ("BUYER_ADD_TO_CART", "BUYER_VIEW_CART"):
        vendor_ctx = state.get("vendor_selection_context")
        if isinstance(vendor_ctx, dict) and not vendor_ctx.get("__reset__"):
            if not slot_has_value(payload.get("product")):
                chosen = vendor_ctx.get("chosen_vendor")
                recovered_product = (
                    chosen.get("name") if isinstance(chosen, dict) else None
                ) or vendor_ctx.get("product")
                if slot_has_value(recovered_product):
                    payload["product"] = recovered_product
                    logger.info(
                        "[MemoryUpdate] Recovered product '%s' from vendor_selection_context",
                        recovered_product,
                    )
            if not slot_has_value(payload.get("quantity")):
                recovered_qty = vendor_ctx.get("requested_quantity")
                if slot_has_value(recovered_qty):
                    payload["quantity"] = recovered_qty
                    logger.info(
                        "[MemoryUpdate] Recovered quantity '%s' from vendor_selection_context",
                        recovered_qty,
                    )
            if not slot_has_value(payload.get("unit")):
                recovered_unit = vendor_ctx.get("requested_unit")
                if slot_has_value(recovered_unit):
                    payload["unit"] = recovered_unit

    # --- ZONE INJECTION from user profile ---
    if not slot_has_value(payload.get("zone")):
        profile_zone = state.get("zone") or state.get("zone_name")
        if slot_has_value(profile_zone):
            payload["zone"] = str(profile_zone).strip()

    # --- SLOT ENRICHMENT: text-based extraction (single pass) ---
    normalized_text = str(
        state.get("normalized_text") or state.get("user_query") or ""
    ).strip()
    _force_clarification = False
    _clarification_reasons = None
    if normalized_text and current_goal and not _commercial_reply_turn:
        payload = await enrich_payload_from_text(
            payload, normalized_text, current_goal, mc_runtime
        )
        # (2026-09-08, P1-3 audit architectural) : `enrich_payload_from_text`
        # pose ce drapeau DANS `payload` (transaction_payload, un canal
        # `merge_dict` conçu pour des données de SLOT conversationnelles —
        # produit/quantité/prix), mais `interpreter/strategy.py` le lit en
        # adresse RACINE (`state.get(...)`). Deux adresses différentes : ce
        # garde-fou de plus haute priorité de `response_strategy` — celui qui
        # doit forcer une clarification quand l'extraction LLM structurée a
        # explicitement échoué (`SlotValidationError`) — n'a jamais pu se
        # déclencher. Fix côté LECTEUR implicite : ce nœud (seul appelant de
        # `enrich_payload_from_text`) relaie le drapeau vers la RACINE de
        # l'état, où `strategy.py` le cherche déjà — jamais une 2e source de
        # vérité, juste le seul point de transit corrigé. Laisser le
        # drapeau DANS `payload` aurait aussi été un risque en soi : ce dict
        # alimente `lookup_arg_value` (résolution d'arguments MCP) et le
        # récapitulatif de confirmation — un booléen de contrôle interne n'a
        # rien à y faire.
        # Phase B1 : « finalement 600 le sachet » — l'enrichissement texte lit « 600 sachets »
        # (QUANTITÉ) alors que « <nombre> le/par <unité> » est un PRIX ; on rétablit la quantité
        # déjà établie et on pose le prix dit, avec sa base, en source explicite.
        if _price_expr is not None:
            _expr_amount, _expr_unit = _price_expr
            payload["price"] = _expr_amount
            payload["price_unit"] = _expr_unit
            if slot_has_value(_established_quantity) and _values_equal(
                payload.get("quantity"), _expr_amount
            ) and not _values_equal(_established_quantity, _expr_amount):
                payload["quantity"] = _established_quantity
                payload["unit"] = _established_unit
        _force_clarification = bool(payload.get("slot_enrichment_force_clarification"))
        payload["slot_enrichment_force_clarification"] = None
        # Même mésadresse, même correctif — `strategy.py:52` relit
        # `clarification_reasons` depuis la RACINE de l'état lui aussi.
        _clarification_reasons = payload.get("clarification_reasons")
        payload["clarification_reasons"] = None

    # --- AG-UI: free-text resolution of ListMenu labels ---
    user_free_text = (
        extracted.get("normalized_text")
        or payload.get("normalized_text")
        or state.get("normalized_text")
        or state.get("user_query")
        or ""
    )
    user_free_text = str(user_free_text or "").strip()
    candidates = state.get("expected_candidates") or []
    if (
        user_free_text
        and candidates
        and "selection_index" not in payload
        and "selected_value" not in payload
        and expected_input == "SELECTION"
    ):
        normalized_user = _normalize_menu_text(user_free_text)
        label_index_map = {
            _normalize_menu_text(label): str(i)
            for i, label in enumerate(candidates, start=1)
        }
        matched_index = label_index_map.get(normalized_user)
        if matched_index:
            payload["selection_index"] = matched_index
            extracted["selection_index"] = matched_index

    # --- AG-UI: Selection index resolution via available_mapping ---
    sel_idx = extracted.get("selection_index") or payload.get("selection_index")
    sel_val = extracted.get("selected_value") or payload.get("selected_value")
    mapping = state.get("available_mapping") or {}
    mapping_kind = working.get("available_mapping_kind")
    session_id = str(state.get("session_id") or state.get("user_phone") or "")
    # (2026-09-09, audit Bloc 2) : `state["menu_snapshot_id"]` est désormais la
    # source CANONIQUE déclarée (voir core/state.py, fix ui_engine 2026-09-09)
    # — `ui_engine.py` l'écrit en top-level ET dans `working_memory` (double
    # écriture délibérée, voir son COMPATIBILITY_SHIM). Le P1-4 ci-dessus
    # (2026-09-08) avait raison de constater que le top-level n'était alors
    # JAMAIS déclaré dans le schéma LangGraph — LangGraph le supprimait donc
    # silencieusement à la traversée du graphe compilé (classe de bug P0-1),
    # rendant `working_memory` la seule copie qui survivait en pratique. Ce
    # n'est plus vrai depuis la déclaration du champ : on lit le canonique en
    # premier, `working_memory` reste un LEGACY FALLBACK pour tout état
    # persisté avant ce changement (checkpoints existants qui n'ont que la
    # copie working_memory) — retirable une fois qu'aucun checkpoint actif ne
    # peut plus en dépendre.
    snapshot_id = state.get("menu_snapshot_id") or working.get("menu_snapshot_id")

    if sel_idx is not None or sel_val is not None:
        resolved_id = None
        if mapping:
            if sel_idx is not None:
                resolved_id = mapping.get(str(sel_idx))
            if resolved_id is None and sel_val is not None:
                resolved_id = mapping.get(str(sel_val))
        if resolved_id is None and snapshot_id:
            selection_token = sel_idx if sel_idx is not None else sel_val
            try:
                # (2026-09-09, Bloc 2 passe finale — Invariant B) : le
                # fallback snapshot n'est autorisé que si le snapshot est
                # PROUVÉ appartenir au menu ACTIF — voir
                # `services/menu_snapshot.py::snapshot_belongs_to_active_menu`
                # pour la preuve d'identité et pourquoi le seul `kind` ne
                # suffisait pas (deux menus successifs du même type
                # partagent leur kind). Sinon : on NE résout PAS (la
                # sélection reste non résolue et le tour retombe sur la
                # clarification/le ré-affichage en aval) — jamais une valeur
                # issue d'un menu qui n'est plus à l'écran.
                snapshot = menu_snapshot_store.get(session_id, snapshot_id)
                belongs, identity_reason = snapshot_belongs_to_active_menu(
                    snapshot,
                    active_mapping=mapping,
                    active_kind=mapping_kind,
                    selection_is_pending=_selection_is_pending(state),
                )
                if belongs:
                    resolved_id = snapshot.mapping.get(str(selection_token))
                elif snapshot is not None:
                    logger.warning(
                        "[MemoryUpdate] Snapshot périmé ignoré (%s) : menu "
                        "actif kind=%s / %d entrée(s), snapshot=%s kind=%s / "
                        "%d entrée(s) (session=%s) — sélection NON résolue "
                        "plutôt que mélangée entre deux menus différents.",
                        identity_reason,
                        mapping_kind,
                        len(mapping or {}),
                        snapshot_id,
                        snapshot.kind,
                        len(snapshot.mapping or {}),
                        session_id,
                    )
            except Exception as snap_exc:
                logger.warning(
                    "[MemoryUpdate] menu_snapshot_store.resolve failed (snapshot=%s token=%s): %s",
                    snapshot_id,
                    selection_token,
                    snap_exc,
                )

        if resolved_id:
            resolved_str = str(resolved_id)
            if mapping_kind in _AUCTION_MAPPING_KINDS:
                payload["auction_id"] = resolved_str
            elif mapping_kind == "bid":
                payload["bid_id"] = resolved_str
            elif mapping_kind == "stock":
                payload["stock_id"] = resolved_str
            elif mapping_kind == "cycle":
                # Sélection d'une production future (MarketOffer) dans la liste
                # des stocks → cible de PRODUCTION_UPDATE_FUTURE (mise à jour de lot).
                payload["cycle_id"] = resolved_str
            elif mapping_kind == "catalog_product":
                # Sélection d'un produit du catalogue → cible de SALES_UPDATE_PRODUCT.
                payload["product_id"] = resolved_str
            elif mapping_kind == "farm":
                payload["farm_id"] = resolved_str
            elif mapping_kind in _ORDER_MAPPING_KINDS:
                payload["order_id"] = resolved_str
            elif mapping_kind == "intent_disambiguation":
                pass
            elif mapping_kind in ("product_vendor", "pricing_tier"):
                # cart_management resolves the vendor/tier by integer position
                # in vendor_selection_context.vendors / tier_selection_context.
                # tiers — it needs the original numeric selection_index, NOT
                # the resolved UUID. Do NOT inject the UUID and do NOT clear
                # selection_index here; cart_management pops it itself once
                # it has successfully located the vendor/tier.
                pass
            else:
                payload["resolved_id"] = resolved_str
            # For product_vendor/pricing_tier, keep selection_index so
            # cart_management can use it. Clearing it here would cause
            # cart_management to skip vendor/tier resolution and re-show the
            # SAME menu on every turn (infinite loop — real incident
            # 2026-08-30: a tier-selection reply was silently resolved
            # against a STALE, unrelated `menu_snapshot_id`/`mapping_kind`
            # left over from an earlier menu in the same conversation,
            # popping `selection_index` before `cart_management` ever saw
            # it — see cart.py's tier menu responses, which now set
            # `available_mapping_kind="pricing_tier"` and clear the stale
            # snapshot/mapping explicitly for exactly this reason).
            if mapping_kind not in ("product_vendor", "pricing_tier"):
                payload["selection_index"] = None
                payload["selected_value"] = None

    # --- `pricing_tiers` : AUCUNE dérivation de quantity/price/unit (voir l'INVARIANT plus bas) ---
    # (historique 2026-08-30 : cette section dérivait un prix « représentatif » et une quantité
    # sommée — supprimé 2026-09-29, incident prod « 60 l de miel »).
    # (2026-08-30) : un producteur donnant plusieurs tarifs/conditionnements
    # ("25 L à 500 fcfa et 40 L à 900 fcfa") sans jamais donner de prix/
    # quantité GLOBAL ne satisfaisait ni `_missing_fields_for_goal`
    # (validator.py) ni `SalesPublishProductPayload.from_payload`
    # (actions/sales_dto.py — lève une ValueError si `price` est absent) : les
    # deux ne connaissent QUE `price`/`quantity` scalaires, jamais
    # `pricing_tiers` — le producteur restait bloqué en boucle sur "prix
    # unitaire ?" malgré avoir déjà tout donné. `pricing_tiers` porte déjà
    # l'info réelle (voir confirmation_summary._format_pricing_tiers qui
    # l'affiche) ; on dérive juste une valeur REPRÉSENTATIVE pour les champs
    # scalaires que le reste du pipeline (validation, contrat, DTO) exige
    # encore — 1er tarif pour price/unit, somme des quantités pour quantity.
    tiers = payload.get("pricing_tiers")
    if isinstance(tiers, list) and tiers:
        # Incident réel (2026-08-30) : un producteur avait déjà répondu
        # QUANTITY="300 litres" à un tour précédent, puis répondu au tour
        # PRICE avec 2 tarifs ("5 L à 500 fcfa et 10 L à 900 fcfa"). Le LLM,
        # tenu de renseigner `quantity` dans le JSON même quand
        # `pricing_tiers` fait le vrai travail, y a mis 5.0 (le 1er tarif) —
        # ce champ racine A ÉCRASÉ le 300 déjà établi via la fusion générique
        # plus haut (`payload[key] = value` pour toute clé non-vide). Le
        # produit publié affichait "5 LITRE dispo" au lieu de 300. Un
        # `pricing_tiers` non-vide EST le vrai contenu d'une réponse PRICE —
        # son `quantity`/`unit` racine n'est qu'un écho du 1er tarif, jamais
        # une nouvelle réponse au slot QUANTITY : restaure la valeur déjà
        # établie AVANT ce tour plutôt que de laisser cet écho la remplacer.
        # Symétrique pour `price`/`price_unit` sur une réponse QUANTITY.
        if expected_input == "PRICE" and slot_has_value(_established_quantity):
            payload["quantity"] = _established_quantity
            if slot_has_value(_established_unit):
                payload["unit"] = _established_unit
        if expected_input == "QUANTITY" and slot_has_value(_established_price):
            payload["price"] = _established_price

        # INVARIANT (fix incident prod « 60 l de miel », 2026-09-29) : `pricing_tiers` est la vérité
        # commerciale ; on n'en DÉRIVE ni `price` / `price_unit` (le prix du 1er palier n'est pas un prix
        # par unité : 700 FCFA le bidon de 5 L n'est pas 700 FCFA/L) ni `quantity` (les contenances
        # 5 L + 9 L ne sont pas un stock de 14 L). Un stock absent est DEMANDÉ (validator :
        # MISSING_AVAILABLE_QUANTITY), un prix absent est porté par les paliers (domain/packaging_tiers_flow.py).

        # SCOPE (hardening 2026-09-29) : cet INVARIANT ne vaut que pour SALES_PUBLISH_PRODUCT, seul goal qui
        # sait porter des paliers de bout en bout (`domain/packaging_tiers_flow.py`). Les AUTRES goals
        # (ex. PRODUCTION_DECLARE_FUTURE : `MarketOffer` n'a ni paliers ni conditionnement) gardent
        # explicitement leur comportement HISTORIQUE — 1er palier pour price/price_unit, somme des
        # quantités pour quantity — ce chantier ne les modifie pas en silence (voir
        # tests/unit/test_memory_tiers_scope_by_goal.py).
        if goal_upper != "SALES_PUBLISH_PRODUCT":

            def _tier_number(raw: Any) -> Optional[float]:
                # Le JSON du LLM type parfois les nombres en chaîne ("500") : accepté.
                if isinstance(raw, (int, float)):
                    return float(raw)
                if isinstance(raw, str):
                    try:
                        return float(raw.strip().replace(",", "."))
                    except ValueError:
                        return None
                return None

            clean_tiers = [t for t in tiers if isinstance(t, dict)]
            if clean_tiers and not slot_has_value(payload.get("price")):
                first_price = _tier_number(clean_tiers[0].get("price"))
                if first_price is not None and first_price > 0:
                    payload["price"] = first_price
                    if not slot_has_value(payload.get("price_unit")):
                        first_unit = clean_tiers[0].get("unit")
                        if first_unit:
                            payload["price_unit"] = first_unit
            if clean_tiers and not slot_has_value(payload.get("quantity")):
                total_qty = 0.0
                all_numeric = True
                for tier in clean_tiers:
                    q = _tier_number(tier.get("quantity"))
                    if q is not None:
                        total_qty += q
                    else:
                        all_numeric = False
                        break
                if all_numeric and total_qty > 0:
                    payload["quantity"] = total_qty
                    if not slot_has_value(payload.get("unit")):
                        first_unit = clean_tiers[0].get("unit")
                        if first_unit:
                            payload["unit"] = canonical_unit_label(first_unit)

    # --- Update stable entities uniquement après complétion ---
    status = str(state.get("status") or "").upper()
    goal_status = str(state.get("goal_status") or "").upper()
    strategy = str(state.get("response_strategy") or "").upper()
    transaction_closed = (
        strategy == "SUCCESS"
        or status in {"COMPLETED", "SUCCESS"}
        or goal_status == "COMPLETED"
    )

    updated_stable = dict(stable)
    if transaction_closed:
        for k in ("product", "unit", "movement_type", "zone"):
            candidate = payload.get(k) or extracted.get(k)
            if slot_has_value(candidate):
                updated_stable[k] = candidate

    # --- Conversation metrics ---
    working["last_event"] = state.get("interpreted_event")
    working["last_confidence"] = float(state.get("interpreter_confidence") or 0.0)
    entity_count = sum(1 for v in payload.values() if slot_has_value(v))
    working["payload_richness"] = entity_count
    # (2026-09-09, audit Bloc 2, Blocker B) : `DISAMBIGUATION_MENU_GOAL_SHIM`
    # n'est plus une clé de contrôle de flux nulle part dans ce nœud — seule
    # exclusion nécessaire ici, pour ne jamais verrouiller un tunnel autour
    # de ce pseudo-goal de sortie (voir core/pending_interaction.py pour le
    # contrat complet, et goal_planner.py::_lock qui applique la même garde
    # côté planner).
    if current_goal and current_goal != DISAMBIGUATION_MENU_GOAL_SHIM:
        working["active_goal"] = current_goal
        working["step_index"] = 0

    existing_draft = (
        draft_payload if draft_payload and not draft_payload.get("__reset__") else {}
    )

    draft_patch: Dict[str, Any] | None = None
    if goal_upper == "BUYER_ADD_TO_CART":
        if not transaction_closed:
            tracked = {
                key: payload.get(key)
                for key in _ADD_TO_CART_DRAFT_FIELDS
                if slot_has_value(payload.get(key))
            }
            if tracked:
                draft_patch = merge_payload(existing_draft, tracked)
        elif existing_draft:
            draft_patch = {"__reset__": True}
    elif transaction_closed and existing_draft:
        draft_patch = {"__reset__": True}

    # Let downstream nodes acknowledge corrections once; response_handlers will clear it.
    if recent_corrections:
        working["recent_corrections"] = recent_corrections
    else:
        working["recent_corrections"] = None

    # (2026-09-03) `quantity`/`unit` ont-ils RÉELLEMENT changé ce tour (vs la
    # valeur établie AVANT toute fusion, capturée en tête de fonction) ? Voir
    # la docstring de `_normalize_quantity_to_kg` — sans ce signal, une vraie
    # correction de quantité/unité pendant une confirmation mettait à jour le
    # contrat d'exécution (`payload["quantity"]`) mais jamais le récapitulatif
    # affiché (`quantity_display`, figé sur la 1ère valeur pour toujours).
    _quantity_or_unit_changed = not _values_equal(
        _established_quantity, payload.get("quantity")
    ) or not _values_equal(_established_unit, payload.get("unit"))
    payload = _normalize_quantity_to_kg(
        payload, refresh_display=_quantity_or_unit_changed
    )
    _mirror_aliases(payload)
    _null_stale_aliases(payload, payload_source)
    _mirror_aliases(updated_stable)
    if draft_patch and not draft_patch.get("__reset__"):
        _mirror_aliases(draft_patch)
    elif existing_draft:
        _mirror_aliases(existing_draft)

    result: Dict[str, Any] = {
        "stable_entities": updated_stable,
        "working_memory": working,
        "current_goal": current_goal,
    }
    # (2026-09-03, durcissement architectural) : tant que `_draft_owns_this_
    # goal`, ce nœud n'affirme AUCUNE valeur pour `transaction_payload` —
    # pas seulement "ne réécrit pas les champs canoniques" (le garde
    # `_skip_merge` ci-dessus), mais littéralement absent du patch retourné.
    # `transaction_payload` est un canal `merge_dict` : une clé absente du
    # patch n'est jamais touchée par le reducer, donc le SEUL écrivain
    # restant pour ce goal est `flows/buyer/procurement_confirmation.py`
    # (au moment CONFIRM, `draft.execution_payload()`). Preuve testable —
    # voir tests/architecture/test_procurement_draft_transactional_contract.py
    # ::TestSingleWriterGuarantee. La variable locale `payload` ci-dessus
    # continue d'exister (bookkeeping interne — zone/stable inheritance,
    # enrich_payload_from_text — inoffensif puisque jamais publié) mais
    # n'est plus la source de vérité pour ce goal.
    if not _draft_owns_this_goal:
        result["transaction_payload"] = payload

    if clear_vendor_ctx:
        result["vendor_selection_context"] = None
        # (2026-09-02, refonte state canonique — invariant "un produit/
        # vendeur différent ne peut jamais réutiliser automatiquement un
        # ancien palier") : `tier_selection_context` n'était JAMAIS remis à
        # zéro ici alors que `vendor_selection_context` l'était — asymétrie
        # confirmée par audit (G-2). Un palier n'a de sens que pour le
        # vendeur/produit qui l'a proposé ; il doit disparaître exactement
        # aux mêmes occasions que `vendor_selection_context`.
        result["tier_selection_context"] = None

    if draft_patch is not None:
        result["draft_payload"] = draft_patch
    elif existing_draft:
        result["draft_payload"] = existing_draft

    compaction_patch = build_compaction_patch(state, tracking_strategy="drop")
    if compaction_patch:
        result.update(compaction_patch)

    if _force_clarification:
        result["slot_enrichment_force_clarification"] = True
        if _clarification_reasons:
            result["clarification_reasons"] = _clarification_reasons

    # --- Convergence FastPath / chemin complet (2026-09-09, micro-passe
    # finale Bloc 2, Sujet B) -------------------------------------------
    # Divergence RÉELLE prouvée par le harness d'équivalence : sur un tour
    # FastPath (`input_interpreter -> memory_update`, `goal_planner` sauté),
    # `goal_status` conserve sa valeur d'entrée ; sur le chemin complet, la
    # RÈGLE 1bis du planner l'écrit à "ACTIVE". Contrairement à `status` —
    # que `validator` réécrit sur TOUTES ses branches de retour, donc
    # normalisé avant tout lecteur — `goal_status` n'est réécrit par
    # `validator` que sur 2 branches sur 5 : la divergence SURVIT jusqu'à
    # `nodes/cleaner.py`, où elle change un résultat MÉTIER (`goal_status`
    # vide => `draft_payload` réinitialisé ; "ACTIVE" => brouillon panier
    # préservé).
    #
    # Ce garde n'est PAS une décision de cycle de vie (elle reste chez
    # `goal_planner`/`validator`) : c'est une invariante de cohérence
    # d'état — « un goal actif ne peut pas avoir de statut de goal vide » —
    # appliquée uniquement quand le champ est ABSENT/VIDE, jamais par
    # écrasement d'une valeur existante (WAITING_INPUT/COMPLETED/... sont
    # laissés intacts).
    if result.get("current_goal") or state.get("current_goal"):
        effective_goal_status = str(
            result.get("goal_status", state.get("goal_status")) or ""
        ).strip()
        if not effective_goal_status:
            result["goal_status"] = "ACTIVE"

    return result
