"""Legacy state -> `CommercialOffer` : l'adaptateur UNIQUE du flow SALES_PUBLISH_PRODUCT.

Phase B1 du chantier « Commercial Quantity & Pricing ». Ce module est le SEUL endroit qui
transforme les champs plats legacy (`product`, `quantity`, `unit`, `price`, `price_unit`,
`quantity_display`…) et le contexte de conversation en `CommercialOffer` ; le `validator`
l'appelle, le draft porte le résultat, la confirmation et l'exécution le lisent. Aucune autre
couche ne reconstruit d'offre (pas de logique de prix dans `memory_update`, dans le rendu ni
dans l'exécuteur).

## Ce que l'adaptateur garantit

1. **Provenance réelle** — jamais `USER_EXPLICIT` pour quelque chose seulement inféré :
   - montant + unité du prix présents dans le TEXTE de ce tour  -> `USER_EXPLICIT` ;
   - montant nu répondant à une question qui FIXE la base (« quel prix par tonne ? ») ->
     `QUESTION_CONTEXT_EXPLICIT` ;
   - `price_unit` fourni par le LLM mais absent du texte -> `LLM_INFERRED` (donc INCOMPLETE) ;
   - montant nu, sans contexte -> base `None` (donc INCOMPLETE).
2. **Une réponse à une question n'est jamais une nouvelle quantité** : « 0,5 litre » répondant à
   « quelle quantité contient un sachet ? » devient le CONTENU du conditionnement.
3. **Invalidation** : changer de produit purge base de prix, conditionnement et provenance ;
   changer l'unité de la quantité sans redonner le prix invalide la base du prix.
4. **Dérivation déterministe** : la représentation normalisée est calculée, jamais devinée.

Module pur : aucune E/S. Réutilise `quantity_unit`/`pricing_tiers` — n'ajoute aucune table de
conversion.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ladini.domain.commercial_offer import (
    CommercialOffer,
    CommercialOfferValidation,
    CommercialQuantity,
    InventoryQuantity,
    PackageDefinition,
    PackageStatus,
    PriceBasis,
    Pricing,
    Provenance,
    base_unit_for,
    convert_commercial_quantity_to_base_unit,
    derive_normalized,
    unit_display,
    validate_offer,
)
from ladini.domain.quantity_unit import (
    _TIER_PACKAGING_WORDS as PACKAGING_WORDS,
)
from ladini.domain.quantity_unit import (
    normalize_unit,
    resolve_product_unit,
)

COMMERCIAL_QUESTION_KIND = "COMMERCIAL_QUESTION"

#: Champs `ENTER_FIELD` posés par ce module (enregistrés `STRUCTURED` dans
#: `core/field_registry.py`).
FIELD_PRICE_BASIS = "price_basis"
FIELD_PACKAGE_SIZE = "package_size"

#: Champs `STRUCTURED` (voir `core/field_registry.py`) dont le résolveur DÉDIÉ est
#: `build_commercial_offer_from_sales_state` (appelé par `nodes/validation.py`) : la réponse est
#: relue depuis le TEXTE dans le contexte de `CommercialQuestion`, pas via une extraction d'entité.
COMMERCIAL_STRUCTURED_FIELDS = frozenset({FIELD_PRICE_BASIS, FIELD_PACKAGE_SIZE})

_TOTAL_CUE_RE = re.compile(
    r"\b(au total|en tout|pour tout|pour l ?ensemble|pour le tout|le tout|"
    r"pour le lot|tout le lot|pour l ?ensemble du lot|lot entier|forfait|globalement)\b"
)
_PACKAGE_WORD_RE = re.compile(r"\b(" + "|".join(sorted(PACKAGING_WORDS, key=len, reverse=True)) + r")\b")
_NUMBER = r"(\d+(?:[.,]\d+)?)"
_CONTENT_UNIT = r"(ml|cl|dl|l|litres?|kg|kilos?|g|grammes?)"
# « 1L le sachet », « 0,5 litre par sachet », « 1 litre pour un sachet »
_CONTENT_BEFORE_RE = re.compile(
    _NUMBER + r"\s*" + _CONTENT_UNIT + r"\s*(?:le|la|par|pour un|pour une|/|chaque|un|une)\s+(?:" +
    "|".join(sorted(PACKAGING_WORDS, key=len, reverse=True)) + r")\b"
)
# « sachet de 0,5 litre », « le sachet fait 1 litre », « un sachet c est 0,5 l », « sachet = 1 l »
_CONTENT_AFTER_RE = re.compile(
    r"\b(?:" + "|".join(sorted(PACKAGING_WORDS, key=len, reverse=True)) + r")\s*"
    r"(?:de|d|fait|contient|pese|vaut|=|c est|cest|c'est|est de|est)?\s*" + _NUMBER + r"\s*" + _CONTENT_UNIT + r"\b"
)
_BARE_CONTENT_RE = re.compile(_NUMBER + r"\s*" + _CONTENT_UNIT + r"?\b")
_WORD_NUMBERS = {"demi": 0.5, "quart": 0.25}
_CURRENCY_RE = re.compile(r"\b(fcfa|cfa|francs?|f)\b")

#: Conversion locale des sous-multiples de contenu (ml/cl/dl/g) — jamais ajoutés aux registres
#: globaux d'unités. Retourne (facteur, unité canonique).
_CONTENT_UNIT_MAP: Dict[str, Tuple[float, str]] = {
    "l": (1.0, "LITRE"), "litre": (1.0, "LITRE"), "litres": (1.0, "LITRE"),
    "ml": (0.001, "LITRE"), "cl": (0.01, "LITRE"), "dl": (0.1, "LITRE"),
    "kg": (1.0, "KG"), "kilo": (1.0, "KG"), "kilos": (1.0, "KG"),
    "g": (0.001, "KG"), "gramme": (0.001, "KG"), "grammes": (0.001, "KG"),
}


def normalize_text(text: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower().replace("œ", "oe"))
    folded = "".join(ch for ch in raw if not unicodedata.combining(ch)).strip()
    # « l'ensemble » -> « l ensemble » : les apostrophes (droite/typographique) sont des espaces,
    # les motifs ci-dessous n'ont ainsi qu'UNE forme à reconnaître.
    return folded.replace("'", " ").replace("’", " ")


# ---------------------------------------------------------------------------
# Contexte de question
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommercialQuestion:
    """Ce que la question POSÉE au tour précédent fixe comme sens de la prochaine réponse.

    Portée par `PendingInteraction.target` (dict sérialisable) — le prochain message est
    interprété DANS ce contexte, jamais en dehors."""

    requested_field: str  # "price" | "price_basis" | "package_size"
    #: `price` : la question dit « par <unité> » -> base fixée par le contexte.
    expected_basis_unit: Optional[str] = None
    package_type: Optional[str] = None  # `package_size`
    content_unit: Optional[str] = None  # `package_size` : unité attendue du contenu
    candidate_amount: Optional[float] = None  # `price_basis` : montant en attente de base

    def to_target(self) -> Dict[str, Any]:
        return {
            "kind": COMMERCIAL_QUESTION_KIND,
            "requested_field": self.requested_field,
            "expected_basis_unit": self.expected_basis_unit,
            "package_type": self.package_type,
            "content_unit": self.content_unit,
            "candidate_amount": self.candidate_amount,
        }

    @classmethod
    def from_target(cls, target: Any) -> Optional["CommercialQuestion"]:
        if not isinstance(target, Mapping) or target.get("kind") != COMMERCIAL_QUESTION_KIND:
            return None
        field = str(target.get("requested_field") or "")
        if field not in ("price", FIELD_PRICE_BASIS, FIELD_PACKAGE_SIZE):
            return None
        amount = target.get("candidate_amount")
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        return cls(
            requested_field=field,
            expected_basis_unit=target.get("expected_basis_unit"),
            package_type=target.get("package_type"),
            content_unit=target.get("content_unit"),
            candidate_amount=amount,
        )

    @property
    def pending_field(self) -> str:
        return self.requested_field  # "price" reste le champ scalaire ; les 2 autres sont STRUCTURED


# ---------------------------------------------------------------------------
# Analyse déterministe du texte (jamais une décision LLM)
# ---------------------------------------------------------------------------


def extract_package_word(text: Any) -> Optional[str]:
    """Mot de conditionnement dit par l'utilisateur, au singulier (« sachets » -> « sachet »)."""
    match = _PACKAGE_WORD_RE.search(normalize_text(text))
    if not match:
        return None
    word = match.group(1)
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


def has_total_cue(text: Any) -> bool:
    return bool(_TOTAL_CUE_RE.search(normalize_text(text)))


def _to_float(raw: str) -> Optional[float]:
    cleaned = raw.replace(" ", "")
    if re.fullmatch(r"\d+\.\d{3}", cleaned):  # « 0.500 » : ambigu (voir quantity_unit._parse_number)
        return None
    try:
        return float(cleaned.replace(",", "."))
    except ValueError:
        return None


def _convert_content(amount: float, unit_token: str) -> Optional[Tuple[float, str]]:
    factor_unit = _CONTENT_UNIT_MAP.get(unit_token.lower())
    if factor_unit is None:
        return None
    return round(amount * factor_unit[0], 6), factor_unit[1]


def parse_package_content(
    text: Any, *, question: Optional[CommercialQuestion] = None
) -> Optional[Tuple[float, str, Provenance]]:
    """Contenu d'UN conditionnement dit dans `text`, ou `None`.

    - phrase qui nomme le conditionnement (« 1L le sachet », « sachet de 0,5 litre »)
      -> `USER_EXPLICIT`, valable à tout moment ;
    - sous la question `package_size` : un nombre (avec ou sans unité) est le CONTENU
      -> `QUESTION_CONTEXT_EXPLICIT`. Sans unité, celle attendue par la question s'applique.
    """
    ntext = normalize_text(text)
    for pattern in (_CONTENT_BEFORE_RE, _CONTENT_AFTER_RE):
        match = pattern.search(ntext)
        if match:
            amount = _to_float(match.group(1))
            converted = _convert_content(amount, match.group(2)) if amount is not None else None
            if converted:
                return converted[0], converted[1], Provenance.USER_EXPLICIT
    if (
        question is not None
        and question.requested_field == FIELD_PACKAGE_SIZE
        and not _CURRENCY_RE.search(ntext)  # « 400 fcfa le litre » est un PRIX, pas un contenu
        and _is_pure_content_reply(ntext)  # « je veux vendre 30 kg de tomates » est une NOUVELLE vente
    ):
        for word, value in _WORD_NUMBERS.items():
            if re.search(rf"\b{word}\b", ntext):
                unit = _first_content_unit(ntext) or question.content_unit
                if unit:
                    return value, unit, Provenance.QUESTION_CONTEXT_EXPLICIT
        numbers = list(_BARE_CONTENT_RE.finditer(ntext))
        if len(numbers) == 1:
            m = numbers[0]
            amount = _to_float(m.group(1))
            if amount is None or amount <= 0:
                return None
            if m.group(2):
                converted = _convert_content(amount, m.group(2))
                if converted:
                    return converted[0], converted[1], Provenance.QUESTION_CONTEXT_EXPLICIT
            elif question.content_unit:
                return amount, question.content_unit, Provenance.QUESTION_CONTEXT_EXPLICIT
    return None


_CONTENT_REPLY_FILLER = frozenset(
    "le la les un une de du des d l par c est ca cest fait contient environ a peu pres je dirais "
    "disons mets mettons chaque chacun et demi stp svp merci ok en gros pour moi cela il elle "
    "contenance taille faut faudrait".split()
)


def _is_pure_content_reply(ntext: str) -> bool:
    """Le message ne fait que DIRE un contenu (« 0,5 litre », « ça fait un demi litre », « 1 l par
    sachet ») : après retrait des nombres, unités de contenu, mots-nombres et mots de liaison, il ne
    reste aucun mot de fond. Sous la question « quelle quantité contient un sachet ? », un message
    qui porte un verbe ou un produit (« je veux vendre 30 kg de tomates ») est une NOUVELLE action
    de l'utilisateur, jamais une réponse — sinon 30 kg de tomates devenaient « sachet de 30 kg »."""
    for word in re.findall(r"[a-z]+", ntext):
        if (
            word in _CONTENT_REPLY_FILLER
            or word in _WORD_NUMBERS
            or word in PACKAGING_WORDS
            or word.rstrip("s") in PACKAGING_WORDS
            or _CONTENT_UNIT_MAP.get(word) is not None
        ):
            continue
        return False
    return True


def _first_content_unit(ntext: str) -> Optional[str]:
    match = re.search(r"\b" + _CONTENT_UNIT + r"\b", ntext)
    if not match:
        return None
    mapped = _CONTENT_UNIT_MAP.get(match.group(1).lower())
    return mapped[1] if mapped else None


_AMOUNT_TOKEN_RE = re.compile(r"\d[\d\s.,]*")
_UNIT_AFTER_PRICE_RE = re.compile(
    r"^\s*(?:fcfa|cfa|francs?|f)?\s*"
    r"(?:/|par\b|chaque\b|le\b|la\b|l\s?['’]|l\b|au\b|a la\b|pour un\b|pour une\b)\s*"
    r"(?:un\s+|une\s+)?([a-z]+)"
)


#: Public alias (Phase B2b) — le motif « <montant> [FCFA] <marqueur de base> <unité> » est partagé avec le flux
#: des bids (`domain/bid_pricing_flow.py`) plutôt que dupliqué.
UNIT_AFTER_PRICE_RE = _UNIT_AFTER_PRICE_RE


def price_unit_next_to_amount(text: Any, amount: Optional[float]) -> Optional[str]:
    """Unité canonique que l'utilisateur a collée AU PRIX (« 500f le sachet » -> SAC,
    « 500 fcfa/kg » -> KG), ou `None`.

    Volontairement STRICT : seule une unité placée APRÈS le montant, introduite par une marque de
    base (« le », « la », « par », « / »…), est la base du prix. Le scanner générique
    `scan_number_candidates` rattache l'unité la plus proche à n'importe quel nombre — dans
    « 200 tonnes de maïs à 500000 » il collait « tonnes » (l'unité de la QUANTITÉ) au prix
    500000 : exactement la confusion que ce module existe pour empêcher."""
    if amount is None:
        return None
    ntext = normalize_text(text)
    for token in _AMOUNT_TOKEN_RE.finditer(ntext):
        value = _to_float(token.group(0).strip().rstrip(".,").replace(" ", ""))
        if value is None or abs(value - amount) > 1e-9:
            continue
        match = _UNIT_AFTER_PRICE_RE.match(ntext[token.end():])
        if not match:
            continue
        word = match.group(1)
        canonical = normalize_unit(word)
        if canonical:
            return str(canonical)
        if word in PACKAGING_WORDS:
            return "SAC"  # tout conditionnement est un « package » ; le mot exact vient de `extract_package_word`
    return None


_PKG_WORDS_ALT = "|".join(sorted(PACKAGING_WORDS, key=len, reverse=True))
# « le sachet à 500 », « le bidon coûte 700 », « sachet de 1 L à 500 », « le sachet = 500 » : le mot de
# conditionnement précède le montant, séparé au plus d'un contenu (« de 1 L ») et d'un lien (« à », « coûte »…).
_PACKAGE_BEFORE_AMOUNT_RE = re.compile(
    r"\b(" + _PKG_WORDS_ALT + r")s?\b"
    r"(?:\s+(?:de|d|fait|contient)\s+" + _NUMBER + r"\s*" + _CONTENT_UNIT + r")?"
    r"\s*(?:est\s+)?(?:a|au prix de|au tarif de|coute|vaut|se vend|=|:|pour)?\s*$"
)


def package_word_before_amount(text: Any, amount: Optional[float]) -> Optional[str]:
    """Mot de conditionnement (singulier) que l'utilisateur a placé JUSTE AVANT le prix
    (« le sachet à 500 » -> « sachet »), ou `None`. Symétrique de `price_unit_next_to_amount`
    (conditionnement APRÈS le montant, « 500 le sachet ») : sans elle, « le sachet à 500 » ne portait
    aucune base et retombait sur le contexte de question (« prix par litre ») -> 500 FCFA/L."""
    if amount is None:
        return None
    ntext = normalize_text(text)
    for token in _AMOUNT_TOKEN_RE.finditer(ntext):
        value = _to_float(token.group(0).strip().rstrip(".,").replace(" ", ""))
        if value is None or abs(value - amount) > 1e-9:
            continue
        match = _PACKAGE_BEFORE_AMOUNT_RE.search(ntext[: token.start()])
        if match:
            return match.group(1)
    return None


def price_expression_in_text(text: Any) -> Optional[Tuple[float, str]]:
    """(montant, unité canonique) de la 1re expression de PRIX « <montant> le/la/par/… <unité> »
    du message (« finalement 600 le sachet » -> (600.0, "SAC")), ou `None`.

    « 600 le sachet » est un prix au sachet, jamais « 600 sachets » : sans ce test, l'enrichissement
    texte générique lisait « 600 » comme la QUANTITÉ, écrasait le prix et perdait le conditionnement."""
    ntext = normalize_text(text)
    for token in _AMOUNT_TOKEN_RE.finditer(ntext):
        value = _to_float(token.group(0).strip().rstrip(".,").replace(" ", ""))
        if value is None:
            continue
        unit = price_unit_next_to_amount(ntext, value)
        if unit:
            return value, unit
    return None


def _units_in_text(text: Any) -> List[str]:
    out: List[str] = []
    for word in re.findall(r"[a-z]+", normalize_text(text)):
        if len(word) < 2:
            # « l », « t », « k » isolés sont des articles/élisions (« l'ensemble »), pas des
            # unités : « pour l'ensemble » lisait « l » comme LITRE.
            continue
        canonical = normalize_unit(word)
        if canonical:
            out.append(canonical)
    return out


@dataclass(frozen=True)
class BasisReply:
    basis: PriceBasis
    basis_unit: Optional[str]
    package_type: Optional[str]
    source: Provenance


def parse_basis_reply(text: Any, *, commercial_unit: Optional[str]) -> Optional[BasisReply]:
    """Réponse à « <montant> FCFA par <unité> ou pour l'ensemble ? ». `None` si la réponse ne
    tranche pas (« oui », « je sais pas ») : l'appelant redemande, jamais ne devine."""
    ntext = normalize_text(text)
    if re.search(r"\d", ntext):
        return None  # un message qui porte son propre nombre est un NOUVEAU prix, pas une base
    if has_total_cue(ntext):
        return BasisReply(PriceBasis.TOTAL_LOT, None, None, Provenance.USER_EXPLICIT)
    package = extract_package_word(ntext)
    units = _units_in_text(ntext)
    if package and any(u in ("SAC", "PANIER") for u in units) or (package and not units):
        return BasisReply(PriceBasis.PER_PACKAGE, None, package, Provenance.USER_EXPLICIT)
    if commercial_unit and commercial_unit.upper() in units:
        return BasisReply(PriceBasis.PER_BASE_UNIT, commercial_unit.upper(), None, Provenance.USER_EXPLICIT)
    if units:
        return BasisReply(PriceBasis.PER_BASE_UNIT, units[0], None, Provenance.USER_EXPLICIT)
    return None


# ---------------------------------------------------------------------------
# Adaptateur legacy -> CommercialOffer
# ---------------------------------------------------------------------------

_PACKAGE_LIKE_UNITS = frozenset({"SAC", "PANIER"})


def _num(value: Any) -> Optional[float]:
    if value in (None, "", [], {}):
        return None
    try:
        return float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _same_product(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and normalize_text(a) == normalize_text(b)


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


@dataclass(frozen=True)
class SalesOfferGateResult:
    """Sortie de `evaluate_sales_offer` : l'offre, son verdict, et — si elle est incomplète —
    la question précise à poser (avec son contexte pour la réponse)."""

    offer: CommercialOffer
    validation: CommercialOfferValidation
    question: Optional[CommercialQuestion] = None
    question_text: Optional[str] = None
    #: noms d'événements d'observabilité à journaliser (voir `nodes/commercial_gate.py`)
    events: Tuple[str, ...] = ()
    #: unité commerciale finale (la taxonomie l'emporte sur le nom du produit)
    resolved_unit: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return bool(self.validation.is_valid)


def build_commercial_offer_from_sales_state(
    payload: Mapping[str, Any],
    *,
    said: Optional[Mapping[str, Any]] = None,
    text: Any = "",
    question: Optional[CommercialQuestion] = None,
    category_config: Optional[Mapping[str, Any]] = None,
) -> Tuple[CommercialOffer, List[str]]:
    """LA fonction centrale de l'étape 2 : champs legacy + contexte -> `CommercialOffer`.

    Retourne `(offer, events)` ; `events` liste ce qui s'est produit (observabilité)."""
    said = said or {}
    events: List[str] = []
    ntext = normalize_text(text)
    product = _first(payload.get("product"))

    previous = CommercialOffer.from_dict(payload.get("commercial_offer"))
    if previous is not None and not _same_product(previous.product, product):
        previous = None  # changement de produit : rien du produit précédent ne survit

    # ------------------------------------------------------------ quantité
    q_amount = _num(_first(payload.get("quantity_display"), payload.get("original_quantity"), payload.get("quantity")))
    raw_unit = _first(payload.get("unit_display"), payload.get("original_unit"), payload.get("unit"))
    # La TAXONOMIE l'emporte sur le nom du produit : config admin si elle existe, sinon règles
    # de repli (élevage => tête, produit dérivé => jamais tête).
    text_unit = next((u for u in _units_in_text(ntext) if u not in _PACKAGE_LIKE_UNITS), None)
    resolved_unit = resolve_product_unit(
        product,
        text_unit=text_unit if "unit" in said or "quantity" in said else None,
        current_unit=raw_unit,
        category_config=dict(category_config) if category_config else None,
    ) or (normalize_unit(str(raw_unit)) if raw_unit else None)
    commercial_unit = normalize_unit(str(resolved_unit)) if resolved_unit else None
    commercial_unit = commercial_unit or (str(resolved_unit).upper() if resolved_unit else None)

    commercial_quantity: Optional[CommercialQuantity] = None
    inventory: Optional[InventoryQuantity] = None
    if q_amount is not None and q_amount > 0 and commercial_unit:
        q_source = Provenance.USER_EXPLICIT if ("quantity" in said or previous is None) else (
            previous.commercial_quantity.source if previous and previous.commercial_quantity else Provenance.USER_EXPLICIT
        )
        commercial_quantity = CommercialQuantity(q_amount, commercial_unit, q_source)
        base_unit = base_unit_for(commercial_unit) or commercial_unit
        converted = convert_commercial_quantity_to_base_unit(commercial_quantity, base_unit)
        if converted is not None and base_unit != commercial_unit:
            inventory = InventoryQuantity(converted[0], base_unit, Provenance.UNIT_CONVERSION)
        else:
            inventory = InventoryQuantity(q_amount, commercial_unit, q_source)

    # ------------------------------------------------------------ prix
    amount = _num(payload.get("price"))
    amount_said = "price" in said and _num(said.get("price")) is not None
    price_unit_raw = normalize_unit(str(payload["price_unit"])) if payload.get("price_unit") else None
    price_unit_in_text = price_unit_next_to_amount(text, amount) if amount is not None else None
    package_word = extract_package_word(ntext)
    unit_changed = bool(
        previous
        and previous.commercial_quantity
        and commercial_quantity
        and previous.commercial_quantity.unit != commercial_quantity.unit
    )

    pricing: Optional[Pricing] = None
    package_type: Optional[str] = previous.package.package_type if previous and previous.package else None
    if amount is not None:
        amount_source = (
            Provenance.USER_EXPLICIT
            if amount_said or previous is None or previous.pricing is None
            else previous.pricing.source
        )
        basis: Optional[PriceBasis] = None
        basis_unit: Optional[str] = None
        basis_source = Provenance.UNKNOWN
        new_price_info = amount_said or (
            question is not None and question.requested_field in ("price", FIELD_PRICE_BASIS)
        )

        reply: Optional[BasisReply] = None
        if question is not None and question.requested_field == FIELD_PRICE_BASIS:
            reply = parse_basis_reply(text, commercial_unit=commercial_unit)
            if reply is None and not amount_said:
                reply = None  # la réponse ne tranche pas : la base reste inconnue (redemande)

        if reply is not None:
            basis, basis_unit, basis_source = reply.basis, reply.basis_unit, reply.source
            package_type = reply.package_type or package_type
        elif new_price_info and has_total_cue(ntext):
            basis, basis_source = PriceBasis.TOTAL_LOT, Provenance.USER_EXPLICIT
        elif new_price_info and price_unit_in_text:
            if price_unit_in_text in _PACKAGE_LIKE_UNITS:
                basis, basis_source = PriceBasis.PER_PACKAGE, Provenance.USER_EXPLICIT
                package_type = package_word or price_unit_in_text.lower()
            else:
                basis, basis_unit, basis_source = PriceBasis.PER_BASE_UNIT, price_unit_in_text, Provenance.USER_EXPLICIT
        elif new_price_info and package_word_before_amount(ntext, amount):
            # Le conditionnement DIT dans la phrase prime sur le contexte de question : « le sachet à
            # 500 » après « quel prix par litre ? » n'est PAS 500 FCFA/L. Le contenu du conditionnement
            # est lu plus bas (`parse_package_content`) ; inconnu -> PACKAGE_REQUIRED, jamais halluciné.
            basis, basis_source = PriceBasis.PER_PACKAGE, Provenance.USER_EXPLICIT
            package_type = package_word_before_amount(ntext, amount)
        elif (
            new_price_info
            and question is not None
            and question.requested_field == "price"
            and question.expected_basis_unit
        ):
            basis, basis_unit = PriceBasis.PER_BASE_UNIT, question.expected_basis_unit.upper()
            basis_source = Provenance.QUESTION_CONTEXT_EXPLICIT
        elif not new_price_info and previous and previous.pricing and previous.pricing.basis and not unit_changed:
            basis, basis_unit = previous.pricing.basis, previous.pricing.basis_unit
            basis_source = previous.pricing.basis_source  # valeur inchangée : provenance d'origine
        elif amount_said and previous and previous.pricing and previous.pricing.basis and not unit_changed:
            # correction du seul montant (« finalement 600 ») pendant qu'un récap est affiché :
            # la base déjà établie (et confirmée à l'écran) reste celle du prix corrigé.
            basis, basis_unit = previous.pricing.basis, previous.pricing.basis_unit
            basis_source = previous.pricing.basis_source
        elif price_unit_raw:
            # `price_unit` fourni par le LLM mais absent du texte de ce tour : INFÉRÉ, pas dit.
            basis = PriceBasis.PER_PACKAGE if price_unit_raw in _PACKAGE_LIKE_UNITS else PriceBasis.PER_BASE_UNIT
            basis_unit = None if basis == PriceBasis.PER_PACKAGE else price_unit_raw
            basis_source = Provenance.LLM_INFERRED
            events.append("PRICE_BASIS_AMBIGUOUS")

        if basis is not None and basis_source.is_execution_safe:
            events.append("PRICE_BASIS_RESOLVED")
        pricing = Pricing(
            amount=amount,
            basis=basis,
            source=amount_source,
            basis_source=basis_source,
            basis_unit=basis_unit,
        )
        if basis is None:
            events.append("PRICE_BASIS_AMBIGUOUS")

    # ------------------------------------------------------------ conditionnement
    package: Optional[PackageDefinition] = None
    if pricing is not None and pricing.basis == PriceBasis.PER_PACKAGE:
        ptype = (package_type or package_word or "conditionnement")
        ptype_key = normalize_text(ptype)
        content_amount: Optional[float] = None
        content_unit: Optional[str] = None
        content_source = Provenance.UNKNOWN
        parsed = parse_package_content(text, question=question)
        if parsed is not None:
            content_amount, content_unit, content_source = parsed
            events.append("PACKAGE_RESOLVED")
        elif (
            previous
            and previous.package
            and previous.package.package_type
            and normalize_text(previous.package.package_type) == ptype_key
            and previous.package.is_content_known
        ):
            content_amount = previous.package.content_amount
            content_unit = previous.package.content_unit
            content_source = previous.package.source
        default_content_unit = base_unit_for(commercial_unit) if commercial_unit else None
        known = content_amount is not None and content_amount > 0 and bool(content_unit)
        package = PackageDefinition(
            package_type=ptype.upper(),
            content_amount=content_amount if known else None,
            content_unit=(content_unit if known else default_content_unit),
            status=PackageStatus.KNOWN if known else PackageStatus.UNKNOWN,
            source=content_source if known else Provenance.UNKNOWN,
        )
        if not known:
            events.append("PACKAGE_REQUIRED")

    offer = CommercialOffer(
        product=product,
        commercial_quantity=commercial_quantity,
        inventory_quantity=inventory,
        pricing=pricing,
        package=package,
    )
    offer = replace(offer, normalized=derive_normalized(offer))
    return offer, events


def offer_from_package_tier(
    payload: Mapping[str, Any],
    tier: Mapping[str, Any],
    *,
    category_config: Optional[Mapping[str, Any]] = None,
) -> Tuple[CommercialOffer, CommercialOfferValidation]:
    """UN palier avec conditionnement explicite (« bidon de 5 L à 700 ») -> le modèle B1 `PER_PACKAGE`.

    La quantité disponible vient du payload (jamais du palier : 5 L est la CONTENANCE du bidon, pas le
    stock) ; le prix (700) est celui DU conditionnement. Aucun prix par unité n'est fabriqué ici."""
    base_payload = {**dict(payload), "price": None, "price_unit": None, "pricing_tiers": None}
    offer, _ = build_commercial_offer_from_sales_state(
        base_payload, said={}, text="", question=None, category_config=category_config
    )
    content = _convert_content(float(tier["quantity"]), str(tier["unit"]))
    if content is None:
        canonical = normalize_unit(str(tier["unit"]))
        content = (float(tier["quantity"]), str(canonical)) if canonical else None
    if content is None:
        raise ValueError(f"unité de contenu inconnue: {tier.get('unit')!r}")
    package = PackageDefinition(
        package_type=str(tier["packaging"]).strip().upper(),
        content_amount=content[0],
        content_unit=content[1],
        status=PackageStatus.KNOWN,
        source=Provenance.USER_EXPLICIT,
    )
    pricing = Pricing(
        amount=float(tier["price"]),
        basis=PriceBasis.PER_PACKAGE,
        source=Provenance.USER_EXPLICIT,
        basis_source=Provenance.USER_EXPLICIT,
        basis_unit=None,
    )
    offer = replace(offer, pricing=pricing, package=package)
    offer = replace(offer, normalized=derive_normalized(offer))
    return offer, validate_offer(offer)


# ---------------------------------------------------------------------------
# Questions de clarification (une seule, précise, avec son contexte de réponse)
# ---------------------------------------------------------------------------


def price_question(commercial_unit: str) -> Tuple[CommercialQuestion, str]:
    label = unit_display(commercial_unit)
    return (
        CommercialQuestion(requested_field="price", expected_basis_unit=commercial_unit.upper()),
        f"Quel est votre prix *par {label}* ? (ex : 500 FCFA par {label})",
    )


def _content_examples(content_unit: Optional[str]) -> str:
    if (content_unit or "").upper() == "KG":
        return "0,5 kg ou 1 kg"
    return "0,5 L ou 1 L"


def clarification_for(
    offer: CommercialOffer, validation: CommercialOfferValidation
) -> Tuple[Optional[CommercialQuestion], Optional[str]]:
    """La question précise pour le PREMIER manque de `validation`."""
    if validation.is_valid or not validation.missing_fields:
        return None, None
    first = validation.missing_fields[0]
    pricing, cq, package = offer.pricing, offer.commercial_quantity, offer.package

    if first == "package_content_amount":
        ptype = (package.package_type if package and package.package_type else "conditionnement").lower()
        content_unit = package.content_unit if package else None
        question = CommercialQuestion(
            requested_field=FIELD_PACKAGE_SIZE,
            package_type=ptype.upper(),
            content_unit=content_unit,
            candidate_amount=pricing.amount if pricing else None,
        )
        text = (
            f"Quelle quantité contient un *{ptype}* ? "
            f"Par exemple {_content_examples(content_unit)}."
        )
        return question, text

    if first == "price_basis" and pricing is not None and cq is not None:
        label = unit_display(cq.unit)
        amount = f"{_fmt_amount(pricing.amount)} FCFA"
        question = CommercialQuestion(
            requested_field=FIELD_PRICE_BASIS,
            expected_basis_unit=cq.unit,
            candidate_amount=pricing.amount,
        )
        text = (
            f"{amount}, c'est *par {label}* ou *pour l'ensemble des "
            f"{_fmt_amount(cq.amount)} {unit_display(cq.unit, cq.amount)}* ?"
        )
        return question, text

    if first == "price" and cq is not None:
        return price_question(cq.unit)
    return None, None


def _fmt_amount(value: float) -> str:
    from ladini.core.formatting import fmt_num

    return str(fmt_num(value))


def evaluate_sales_offer(
    payload: Mapping[str, Any],
    *,
    said: Optional[Mapping[str, Any]] = None,
    text: Any = "",
    question: Optional[CommercialQuestion] = None,
    category_config: Optional[Mapping[str, Any]] = None,
) -> SalesOfferGateResult:
    """Adaptateur + validateur + question, en un appel — ce que le `validator` consomme."""
    offer, events = build_commercial_offer_from_sales_state(
        payload, said=said, text=text, question=question, category_config=category_config
    )
    validation = validate_offer(offer)
    question_out, question_text = clarification_for(offer, validation)
    all_events = list(events)
    all_events.append("COMMERCIAL_OFFER_PARSED")
    if not validation.is_valid:
        all_events.append("COMMERCIAL_OFFER_INCOMPLETE" if validation.status == "INCOMPLETE" else "COMMERCIAL_OFFER_INVALID")
    return SalesOfferGateResult(
        offer=offer,
        validation=validation,
        question=question_out,
        question_text=question_text,
        events=tuple(dict.fromkeys(all_events)),
        resolved_unit=offer.commercial_quantity.unit if offer.commercial_quantity else None,
    )


__all__ = [
    "COMMERCIAL_QUESTION_KIND",
    "FIELD_PRICE_BASIS",
    "FIELD_PACKAGE_SIZE",
    "COMMERCIAL_STRUCTURED_FIELDS",
    "CommercialQuestion",
    "BasisReply",
    "SalesOfferGateResult",
    "normalize_text",
    "extract_package_word",
    "has_total_cue",
    "parse_package_content",
    "parse_basis_reply",
    "price_unit_next_to_amount",
    "package_word_before_amount",
    "offer_from_package_tier",
    "UNIT_AFTER_PRICE_RE",
    "price_expression_in_text",
    "build_commercial_offer_from_sales_state",
    "price_question",
    "clarification_for",
    "evaluate_sales_offer",
]
