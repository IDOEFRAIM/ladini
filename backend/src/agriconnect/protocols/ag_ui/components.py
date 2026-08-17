import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional

# ═══════════════════════════════════════════════════════════
# ENUMS (Standardisés en MAJUSCULES pour les types d'action)
# ═══════════════════════════════════════════════════════════


class ComponentType(str, Enum):
    TEXT = "text"
    CARD = "card"
    ACTION = "action"
    LIST_PICKER = "list_picker"
    FORM_FIELD = "form_field"
    CHART = "chart"
    ALERT = "alert"
    USER_APPROVAL = "user_approval"
    CONTEXT_REQUEST = "context_request"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class ActionType(str, Enum):
    SELL = "SELL"
    BUY = "BUY"
    CONTACT = "CONTACT"
    NAVIGATE = "NAVIGATE"
    CONFIRM = "CONFIRM"
    CANCEL = "CANCEL"
    CALL_EXPERT = "CALL_EXPERT"
    VIEW_DETAIL = "VIEW_DETAIL"


# ═══════════════════════════════════════════════════════════
# BASE COMPONENT
# ═══════════════════════════════════════════════════════════


@dataclass
class AGUIComponent:
    """Base class avec gestion sécurisée de la sérialisation."""

    type: ComponentType
    id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Sérialisation récursive gérant les Enums."""
        return self._serialize_value(asdict(self))

    def _serialize_value(self, value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {k: self._serialize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._serialize_value(v) for v in value]
        return value

    def clone(self, **changes):
        """Version sécurisée de la copie (évite les bugs asdict/Enum)."""
        return replace(self, **changes)


# ═══════════════════════════════════════════════════════════
# UI COMPONENTS
# ═══════════════════════════════════════════════════════════


@dataclass
class TextBlock(AGUIComponent):
    type: ComponentType = ComponentType.TEXT
    content: str = ""
    format: str = "plain"  # plain | markdown | html
    voice_text: str = ""

    def __post_init__(self):
        if not self.voice_text:
            self.voice_text = self.content


@dataclass
class ActionButton(AGUIComponent):
    type: ComponentType = ComponentType.ACTION
    label: str = ""
    action_type: ActionType = ActionType.NAVIGATE
    payload: Dict[str, Any] = field(default_factory=dict)
    confirm_required: bool = False


@dataclass
class Card(AGUIComponent):
    type: ComponentType = ComponentType.CARD
    title: str = ""
    subtitle: str = ""
    body: str = ""
    image_url: str = ""
    fields: List[Dict[str, str]] = field(default_factory=list)
    actions: List[ActionButton] = field(default_factory=list)
    severity: Optional[Severity] = None


@dataclass
class ListPicker(AGUIComponent):
    type: ComponentType = ComponentType.LIST_PICKER
    title: str = ""
    items: List[Dict[str, str]] = field(default_factory=list)
    multi_select: bool = False


@dataclass
class FormField(AGUIComponent):
    type: ComponentType = ComponentType.FORM_FIELD
    label: str = ""
    field_type: str = "text"
    placeholder: str = ""
    required: bool = True
    validation: Dict[str, Any] = field(default_factory=dict)
    options: List[str] = field(default_factory=list)


@dataclass
class ChartData(AGUIComponent):
    type: ComponentType = ComponentType.CHART
    chart_type: str = "line"
    title: str = ""
    labels: List[str] = field(default_factory=list)
    datasets: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AlertBanner(AGUIComponent):
    type: ComponentType = ComponentType.ALERT
    title: str = ""
    message: str = ""
    severity: Severity = Severity.INFO
    zone: str = ""
    expires_at: str = ""
    actions: List[ActionButton] = field(default_factory=list)

    def __post_init__(self):
        self.zone = self.zone.lower()


# ═══════════════════════════════════════════════════════════
# HITL & CONTEXT
# ═══════════════════════════════════════════════════════════


@dataclass
class UserApproval(AGUIComponent):
    """Composant HITL (Human-In-The-Loop)."""

    type: ComponentType = ComponentType.USER_APPROVAL
    action_id: str = ""
    action_summary: str = ""
    risk_level: Severity = Severity.WARNING
    requires_validation: bool = True
    payload: Dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 300
    callback_url: str = ""


@dataclass
class ContextRequest(AGUIComponent):
    """Context Elicitation (Anti-hallucination)."""

    type: ComponentType = ComponentType.CONTEXT_REQUEST
    missing_fields: List[Dict[str, str]] = field(default_factory=list)
    message: str = ""


# ═══════════════════════════════════════════════════════════
# AGRI RESPONSE WRAPPER
# ═══════════════════════════════════════════════════════════


class AgriResponse:
    def __init__(self, agent_name: str = ""):
        self.agent = agent_name
        self.components: List[AGUIComponent] = []
        self.voice_summary: str = ""
        self.raw_text: str = ""
        self.metadata: Dict[str, Any] = {}

    def add(self, component: AGUIComponent) -> "AgriResponse":
        self.components.append(component)
        return self

    def add_text(self, content: str, voice: str = "") -> "AgriResponse":
        return self.add(TextBlock(content=content, voice_text=voice))

    def add_card(self, title: str, body: str, **kwargs) -> "AgriResponse":
        return self.add(Card(title=title, body=body, **kwargs))

    def add_approval(self, action_id: str, summary: str, **kwargs) -> "AgriResponse":
        self.metadata["requires_validation"] = True
        self.metadata["pending_action_id"] = action_id
        return self.add(
            UserApproval(action_id=action_id, action_summary=summary, **kwargs)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "components": [c.to_dict() for c in self.components],
            "voice_summary": self.voice_summary,
            "raw_text": self.raw_text,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
