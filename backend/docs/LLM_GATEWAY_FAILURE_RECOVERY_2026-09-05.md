# LLM GATEWAY — FAILURE RECOVERY & DEGRADED MODE (2026-09-05)

> **Addendum (même jour, log de production réel)** : après le fix §3
> ci-dessous, un second tour de logs live a révélé deux problèmes distincts
> — voir §6.

## 1. Incident

```
Utilisateur : « je veux voir les enchères »
Système     : « Je n'ai pas bien saisi... »
```

Logs :

```
security_moderation ≈ 10s
input_interpreter
  bedrock_gateway:deepseek.v3.2      -> HTTP 401 (security token expired)
  bedrock_gateway:openai.gpt-oss-120b -> HTTP 401 (security token expired)
  groq:llama-3.3-70b-versatile        -> application_error
                                          (get_groq_sdk() refuse : LLM_PROVIDER != groq/auto)
  -> all candidates failed -> UNKNOWN
clarification_node
  -> mêmes providers -> mêmes échecs -> clarification générique
```

## 2. Root cause

`registry.py` déclare la chaîne de repli REASONING comme trois candidats
**indépendants**, chacun portant explicitement son provider
(`.env` : `LLM_REASONING_PRIMARY=bedrock_gateway:...`,
`LLM_REASONING_FALLBACK_2=groq:...`) — c'est le **Modèle A** du brief :
chaque candidat s'authentifie avec ses propres identifiants, sans rapport
avec `LLM_PROVIDER`.

Mais `core/get_llm.py::get_groq_sdk()` portait un garde hérité de l'époque
où lui seul construisait LE client LLM du process :

```python
provider = settings.LLM_PROVIDER
if provider not in {"groq", "default", "auto"}:
    raise RuntimeError("... LLM_PROVIDER=groq (ou auto) requis ...")
```

Avec `LLM_PROVIDER=bedrock` en production (passerelle Bedrock primaire),
ce garde refusait la construction du client Groq pour **n'importe quel**
appelant — y compris `llm_gateway/gateway.py::_default_client_for_provider`,
qui l'invoque directement pour tout candidat `groq:*` de la chaîne de
repli, quel que soit `LLM_PROVIDER`.

Conséquence en cascade :
1. Le repli groq — valide, identifiant présent — n'était **jamais tenté**.
2. Son échec (`RuntimeError`) ne contient aucun des fragments reconnus par
   `error_classification.py::_looks_like_config_error` → classé
   `APPLICATION` par défaut, pas `CONFIG` → ne touche jamais la santé du
   candidat → **retenté à l'identique à chaque tour, indéfiniment**.
3. `input_interpreter`, tous candidats épuisés, retombait sur un `UNKNOWN`
   générique.
4. `clarification_node` retentait le **même appel LLM** sur le **même
   tour** → mêmes échecs → réponse « je n'ai pas bien saisi » alors que la
   vraie cause était une panne d'infrastructure, pas une incompréhension.

Second appelant touché, silencieux celui-là : `get_llm()` (branche
`LLM_PROVIDER=bedrock`) construit son propre `fallback_client = get_groq_sdk()`
pour un repli inter-provider documenté — ce garde le faisait échouer aussi,
avalé par un `except Exception` local, rendant ce mécanisme inopérant dès
que `LLM_PROVIDER != groq`.

## 3. Fix

### 3.1 `core/get_llm.py::get_groq_sdk()`

Le garde `LLM_PROVIDER` est retiré. Seule condition restante :
`settings.llm_api_key` (GROQ_API_KEY/AGRICONNECT_APIKEY) présent — la vraie
question. `LLM_PROVIDER` continue de gouverner **uniquement** le client
legacy unique construit par `get_llm()` ; il n'a plus voix dans la
construction d'un candidat individuel du Gateway.

### 3.2 Pré-check de disponibilité — `llm_gateway/availability.py` (nouveau)

```python
is_candidate_usable(candidate, settings) -> AvailabilityCheck(usable, reason)
```

Appelé dans `gateway.py::complete()` **avant** le disjoncteur, sans appel
réseau :

| Provider | Condition | Sinon |
|---|---|---|
| `groq` | `settings.llm_api_key` non vide | `CREDENTIAL_MISSING` |
| `bedrock_gateway` | `settings.OPENAI_API_KEY` non vide | `CREDENTIAL_MISSING` |
| `bedrock_native` | toujours usable (chaîne boto3 implicite — limitation documentée) | — |
| provider inconnu de ce module | **fail-open** (`usable=True`) | `registry.py` a déjà filtré les providers inconnus en amont ; un candidat non reconnu ICI n'est pas une preuve d'impossibilité |
| `MOCK_EXTERNAL_APIS=true` | toujours usable | cohérent avec `core/get_llm.py::_MockGroqClient` |

Un candidat sauté par ce pré-check **ne touche jamais** la santé/le
disjoncteur (`config_error` reste `False`, `total_requests` inchangé) — ce
n'est pas une panne, c'est une impossibilité structurelle connue à l'avance.
Un token expiré (401 découvert à l'appel) **reste** du ressort du
disjoncteur, classé `CONFIG` comme avant — le pré-check ne remplace rien,
il évite seulement les appels qui n'ont structurellement aucun sens.

### 3.3 Raison structurée d'épuisement — `LLMGatewayExhausted.reason`

```python
NO_CANDIDATES_CONFIGURED   # profil sans aucun candidat en configuration
ALL_CANDIDATES_UNAVAILABLE # zéro tentative réseau — tout était sauté (config/circuit)
ALL_ATTEMPTS_FAILED        # au moins un candidat réellement appelé, tous ont échoué
BUDGET_EXHAUSTED           # budget épuisé avant la première tentative viable
```

Propagée dans `raw_analysis.gateway_reason` par `input_interpreter`
(`interpreter/routing.py`) — observabilité seulement, ne change pas
`unknown_reason` (déjà `TECHNICAL_FAILURE` pour ce chemin, voir
`interpreter/interpreter_result.py`, préexistant).

### 3.4 Zéro second appel LLM inutile — `clarification.py`

`clarification_node` court-circuite désormais son appel LLM quand
`state.get("unknown_reason") == "TECHNICAL_FAILURE"` — signal déjà posé par
`input_interpreter`/`InterpreterResult` (préexistant, jamais consommé
jusqu'ici) — et rend un repli déterministe honnête :

> 🔧 Notre assistant intelligent est momentanément indisponible. Réessayez
> dans quelques instants — vos commandes et votre panier restent intacts.

Jamais « je n'ai pas bien saisi » pour une panne d'infrastructure : c'est un
mensonge sur la cause, l'utilisateur n'a rien mal formulé. Une ambiguïté de
contenu **réelle** (`unknown_reason` absent ou `AMBIGUOUS`) garde son
comportement adaptatif existant, inchangé.

### 3.5 Endpoint santé — `api/routes/admin.py::/admin/llm/health`

Intègre le même pré-check : un candidat `CLOSED` (jamais tenté) mais
structurellement inutilisable n'est plus présenté comme `selected`/sain.
Nouveau champ par candidat `"availability": "OK" | "CREDENTIAL_MISSING"`.
Aucune valeur de secret n'est jamais exposée (vérifié par test).

## 4. Ce qui N'A PAS changé

- Le disjoncteur (`circuit_breaker.py`/`health_registry.py`) : mécanique
  CLOSED → OPEN → cooldown → HALF_OPEN → CLOSED intacte.
- La classification d'erreur (`error_classification.py`) : TRANSIENT/
  CONFIG/APPLICATION inchangée — le pré-check est une étape **antérieure**,
  pas un remplacement.
- Le budget global par tour, `_MIN_VIABLE_SECONDS`/`_SAFETY_MARGIN_SECONDS`.
- `PendingInteraction`, `ResponsePlan`, MCP, l'interpréteur (fast-paths,
  `_degraded_fallback`, prompts LLM) : aucune ligne touchée.

## 5. Tests

| Fichier | Rôle |
|---|---|
| `tests/unit/llm_gateway/test_availability.py` | pré-check pur, par provider, MOCK_EXTERNAL_APIS, fail-open provider inconnu |
| `tests/unit/llm_gateway/test_gateway.py::TestProviderAvailabilityPrecheck` | zéro appel réseau + zéro impact santé pour un candidat sauté |
| `tests/unit/llm_gateway/test_gateway.py::TestExhaustedReasonDistinguishesWhy` | les 3 raisons d'épuisement, cas par cas |
| `tests/unit/test_get_llm_provider_independence.py` | `get_groq_sdk()` indépendant de `LLM_PROVIDER` ; garde crédential réel préservé ; le fallback `bedrock` fonctionne réellement désormais |
| `tests/unit/test_clarification_node.py::TestTechnicalFailureSkipsASecondWastedLlmCall` | zéro appel LLM sur `TECHNICAL_FAILURE`, comportement normal préservé sinon |
| `tests/unit/test_admin_llm_health_availability.py` | endpoint santé reflète `CREDENTIAL_MISSING`, ne fuite aucun secret |
| `tests/integration/test_llm_gateway_provider_config_incident_2026_09_05.py` | reproduction **de bout en bout** (VRAIE `LLMGateway`, `input_interpreter` PUIS `clarification_node`) de l'incident réel, avant/après |

Suite complète (`pytest tests -q`) : les 4 échecs historiques
(`test_create_auction_catalog_gate.py`, date codée en dur) inchangés. 2
échecs supplémentaires détectés, **préexistants et sans rapport** (confirmés
via `git stash` — présents avant ce chantier), dans
`tests/architecture/test_order_mutations_require_ownership.py` : deux faux
positifs d'un test à base de grep, qui matche des mentions en prose dans la
docstring de `infrastructure/mcp/exposure.py` (Phase 8, session antérieure)
plutôt qu'un vrai appelant. Hors périmètre de ce chantier (MCP explicitement
exclu, §4/§34) — signalé séparément, non corrigé ici.

## 6. Addendum — log de production réel post-fix (16:39/17:49)

Le fix §3 a été observé fonctionner exactement comme prévu en production
sur ce même tour :

```
LLM_GATEWAY_EXHAUSTED | profile=REASONING reason=ALL_ATTEMPTS_FAILED attempts=1/3
[ClarificationNode] LLM déjà indisponible ce tour (unknown_reason=TECHNICAL_FAILURE)
  — repli déterministe, aucun second appel Gateway
TWILIO_SEND | body_preview='🔧 Notre assistant intelligent est momentanément indisponible...'
```

Un seul appel Gateway sur tout le tour (pas deux), message honnête envoyé —
le comportement cible. Mais ce log a révélé DEUX problèmes réels distincts,
non couverts par le fix initial :

### 6.1 Modèles Groq décommissionnés (root cause du 404 observé)

```
LLM_MODEL_CONFIG_ERROR | candidate=groq:llama-3.3-70b-versatile
  | Error code: 404 - model `llama-3.3-70b-versatile` does not exist
  or you do not have access to it.
```

Confirmé : Groq a décommissionné `llama-3.3-70b-versatile` ET
`llama-3.1-8b-instant` le **2026-06-17** (tiers gratuit/développeur —
[console.groq.com/docs/deprecations](https://console.groq.com/docs/deprecations)),
remplacements recommandés par Groq lui-même :
[`openai/gpt-oss-120b`](https://console.groq.com/docs/model/openai/gpt-oss-120b)
et [`openai/gpt-oss-20b`](https://console.groq.com/docs/model/openai/gpt-oss-20b).

**Fix** : `core/settings.py` et `.env.example` mis à jour —
`LLM_REASONING_FALLBACK_2=groq:openai/gpt-oss-120b` (même famille de modèle
que `bedrock_gateway:openai.gpt-oss-120b`, vraie redondance multi-provider
sur le même modèle) et `LLM_FAST_FALLBACK_1=groq:openai/gpt-oss-20b`.
**Limite assumée** : si le `.env` réellement déployé définit ces variables
explicitement (probable, vu qu'il a fallu ce log pour le découvrir), il doit
être mis à jour manuellement à cet endroit précis — impossible pour ce
chantier d'éditer un secret d'exécution qu'il ne voit pas.

### 6.2 `CONFIG_ERROR` sans recovery (bug distinct, découvert en creusant)

Second tour (17:49) : les **trois** candidats REASONING sont désormais
`CONFIG_ERROR` (les deux `bedrock_gateway` restent en 401 depuis
l'incident initial, jamais résolus côté token). En lisant
`mark_config_error()`/`CircuitBreaker.decide()` : un candidat
`config_error=True` était **skippé indéfiniment**, sans jamais fixer
`cooldown_until` ni jamais être remis à `False` sur un succès — sa seule
porte de sortie était le TTL Redis de 7 jours ou un flush manuel. Même
après rotation du token Bedrock par les opérateurs, le système ne
l'aurait jamais découvert tout seul.

**Fix** (réutilise le disjoncteur existant, §9 — ne le reconstruit pas) :
- `mark_config_error(..., cooldown_seconds=...)` fixe désormais
  `cooldown_until`, comme un OPEN ordinaire.
- `CircuitBreaker.decide()` : un `config_error` suit maintenant le MÊME
  cooldown + verrou de probe HALF_OPEN qu'un OPEN transitoire (motif de SKIP
  distinct, `"CONFIG_ERROR"`, tant que le cooldown n'est pas écoulé).
- `HealthRegistry._apply_success()` : un probe HALF_OPEN réussi (ou un
  succès direct en OPEN) efface désormais `config_error`/
  `config_error_message` — sans quoi la reprise resterait invisible malgré
  `state=CLOSED`.

Testé : `tests/unit/llm_gateway/test_gateway.py::TestConfigErrorEventuallyRecovers`
(recovery après cooldown écoulé + non-régression : toujours sauté AVANT le
cooldown, zéro tempête de requêtes).

## 7. Reproduction de l'incident

```
Avant : bedrock_gateway x2 (401) -> groq refusé par LLM_PROVIDER -> UNKNOWN
         -> clarification_node retente les mêmes 3 candidats -> même échec
         -> "Je n'ai pas bien saisi..."

Après : bedrock_gateway x2 (401, classé CONFIG, disjoncteur ouvert) -> groq
         (identifiant valide, jamais bloqué) -> RÉUSSIT -> intention exploitée.

         Si TOUS les candidats sont réellement inutilisables : UNKNOWN avec
         unknown_reason=TECHNICAL_FAILURE -> clarification_node NE rappelle
         PAS le Gateway -> repli honnête "service temporairement
         indisponible, réessayez" -> zéro appel réseau supplémentaire.
```

---

```
LLM GATEWAY STATUS

Provider availability:        PASS
Credential failure handling:  PASS
Fallback:                     PASS
Circuit breaker:               PASS
Global LLM budget:            PASS (inchangé)
Degraded mode:                PASS
No duplicate clarification:   PASS
Structured failure reason:    PASS
Recovery:                     PASS (CONFIG_ERROR probé de nouveau après cooldown, §6.2)
Production config:            PASS (modèles Groq décommissionnés remplacés, §6.1)
Regression:                   4 échecs historiques (date codée en dur) +
                               2 échecs préexistants sans rapport (MCP
                               exposure.py, hors périmètre) — 0 nouveau
```

```
INCIDENT REPRODUCTION

User:
« je veux voir les enchères »

Before:
UNKNOWN / clarification générique (2 appels LLM gaspillés sur le même tour) ;
puis, une fois la cause provider corrigée, un candidat CONFIG_ERROR
(401/404) restait bloqué indéfiniment sans recovery.

After:
Cas nominal : le repli groq (identifiant valide, modèle à jour) répond —
plus de UNKNOWN. Cas panne totale réelle : UNKNOWN avec
unknown_reason=TECHNICAL_FAILURE, UN SEUL appel Gateway sur tout le tour,
repli honnête "service temporairement indisponible" — observé tel quel en
production (§6). Un candidat CONFIG_ERROR se re-sonde désormais et se
rétablit tout seul dès que la cause réelle (token/modèle) est corrigée.

Root cause (§2, incident initial):
`core/get_llm.py::get_groq_sdk()` refusait de construire un client Groq
tant que `settings.LLM_PROVIDER != "groq"/"auto"` — condition héritée du
client legacy unique, sans rapport avec la disponibilité réelle de
l'identifiant Groq pour un candidat INDÉPENDANT du LLM Gateway (Modèle A).

Root cause additionnelles (§6, log production post-fix):
Groq a décommissionné `llama-3.3-70b-versatile`/`llama-3.1-8b-instant`
(2026-06-17) ; et `CONFIG_ERROR` n'avait aucun chemin de retour
(`cooldown_until` jamais fixé, jamais effacé sur succès).

Fix:
(1) Garde `LLM_PROVIDER` retiré — seule la présence de l'identifiant compte.
(2) Pré-check de disponibilité (`availability.py`) avant tout appel réseau,
    sans toucher au disjoncteur.
(3) `LLMGatewayExhausted.reason` structuré (4 catégories).
(4) `clarification_node` ne rappelle plus le Gateway sur
    `unknown_reason=TECHNICAL_FAILURE` — repli déterministe honnête.
(5) `/admin/llm/health` reflète la disponibilité réelle (sans exposer de
    secret).
(6) Modèles Groq décommissionnés remplacés par les repli recommandés par
    Groq (`openai/gpt-oss-120b`/`openai/gpt-oss-20b`).
(7) `CONFIG_ERROR` réutilise désormais le cooldown + probe HALF_OPEN du
    disjoncteur existant — recovery automatique, plus de blocage
    indéfini.
```
