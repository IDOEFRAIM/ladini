# Analyse profonde & failles observées

## Garde-fou TaskHandler inadapté à la majorité des intents producteurs
* TaskPayload.is_ready() impose price et quantity non nuls avant tout passage à COMPLETED. @backend/src/agriconnect/agents/task_handler.py#32-145

* _build_task_payload injecte ces champs, même lorsqu’ils ne sont pas requis (ex. FARM_CREATE, STOCK_DELETE, PROFILE_SET_PREFS). Résultat : toute écriture sans duo prix/quantité finit par lever Payload incomplet: completion interdite, comme vu sur SALES_LIST_ORDERS. @backend/src/agriconnect/graphs/agents/market_coach/nodes/executor.py#464-580

* Correctif proposé :

  * Restreindre le passage par TaskHandler aux seules intentions marquées comme « price+quantity critical » (ex. publier une offre, enregistrer une vente).
  * Ou bien enrichir TaskPayload avec un indicateur requires_price, requires_quantity basé sur INTENT_CONFIG et assouplir _transition pour les intents qui ne demandent qu’un sous-ensemble d’attributs.

## Auto-provisionnement des fermes déclenché pour des intents lecture

* FARM_CRITICAL_GOALS inclut FARM_GET_MY_LIST, MARKET_GET_REQUESTS et d’autres lectures. @backend/src/agriconnect/graphs/agents/market_coach/core/base.py#31-47
* Dès que goal appartient à cet ensemble et qu’aucun farm_id n’est présent, ensure_farm_node tente de créer une ferme par défaut, même si l’utilisateur voulait simplement consulter une liste. @backend/src/agriconnect/graphs/agents/market_coach/flows/producer/farm_logic.py#20-101
* Correctif proposé :
  * Séparer les ensembles « lecture » / « écriture » critiques : seuls les intents nécessitant réellement un farm_id pour écrire devraient figurer dans FARM_CRITICAL_GOALS.
  * Laisser les lectures afficher un message clair (« créez d’abord une ferme ») au lieu de créer silencieusement une exploitation.

## Perte systématique d’auction_id pour MARKET_GET_REQUEST_DETAIL

* `prep_market_get_request_detail` exige `auction_id`… mais ne l’envoie jamais au MCP tool (seuls phone et status sont transmis). @backend/src/agriconnect/graphs/agents/market_coach/actions/sales.py#37-52

* producer_context_resolver ne possède aucune branche pour aider à résoudre cet auction_id, contrairement aux autres intents marché. @backend/src/agriconnect/graphs/agents/market_coach/flows/producer/flow.py#470-495

* Conséquence : impossible pour un producteur de cibler une demande précise, le backend renvoie les bids globaux.

* Correctif proposé :
  * Ajouter un resolver similaire à _resolve_auction mais orienté « mes demandes » (filtrer sur les auctions publiées par le producteur, proposer un menu et injecter auction_id).
  * Passer auction_id à get_auctions_bids pour ne charger que la demande sélectionnée.

## Dissociation entre cache des fermes et résolution runtime

* ProducerContext expose user_farms_cache et validation.py s’en sert pour éviter des allers-retours. @backend/src/agriconnect/graphs/agents/market_coach/flows/producer/state.py#17-24 et @backend/src/agriconnect/graphs/agents/market_coach/nodes/validation.py#432-487

* _resolve_default_farm, lui, ignore totalement ce cache et relance systématiquement get_farms, même lorsque l’information est déjà en mémoire. @backend/src/agriconnect/graphs/agents/market_coach/flows/producer/flow.py#256-346
Conséquence : requêtes redondantes, risque de divergence entre menus proposés par le validator et ceux du resolver.

* Correctif proposé :
    * Injecter state (ou au moins user_farms_cache) dans _resolve_default_farm, réutiliser la liste préchargée et ne retomber sur MCP qu’en cas d’absence/expirations de cache.

## MARKET_GET_REQUEST_DETAIL classé « BUYER », donc jamais proposé aux producteurs

* INTENT_ROLE marque `MARKET_GET_REQUEST_DETAIL` comme BUYER-only alors que le dispatcher et la doc décrivent une action producteur (voir _prep_market_get_request_detail). @backend/src/agriconnect/graphs/agents/market_coach/interpreter/intent.py#481-551
* Conséquence : le Goal Planner n’assignera jamais ce goal à un producteur, le DomainRouter ne le routa pas, et les fast-paths producteurs n’ont aucun moyen d’y accéder.
* Correctif proposé : reclasser l’intent en `PRODUCER`, mettre à jour les fast-paths et relancer un test de régression sur les requêtes « voir les offres reçues ».

## Trust score inaccessible (UUID attendu, mais on envoie un téléphone)

* `prep_profile_get_trust` passe `{"user_id": phone}` à `get_trust_score`. @backend/src/agriconnect/graphs/agents/market_coach/actions/profile.py#16-25
* Côté Intelligence, `get_user_trust_score` tente systématiquement de caster `user_id` en UUID et lève une ValueError lorsque la valeur ne respecte pas le format. @backend/src/agriconnect/services/database/intelligence.py#323-369
* Résultat : tous les producteurs reçoivent un échec technique lorsqu’ils demandent leur note de confiance.
* Correctif proposé : fournir l’UUID réel de l’utilisateur (`state.user_id`) dans le dispatcher ou exposer un outil `get_trust_score_by_phone` si l’API veut rester orientée téléphone.

## ensure_farm_node sélectionne arbitrairement la première ferme

* Lorsque l’intent n’a pas été résolu par `_resolve_default_farm` (car non listé dans GOALS_NEEDING_FARM_ID), `ensure_farm_node` récupère les fermes, puis `_extract_farm_id` renvoie systématiquement le premier élément sans demander l’avis de l’utilisateur. @backend/src/agriconnect/graphs/agents/market_coach/flows/producer/farm_logic.py#10-101
* Sur un producteur multi-fermes, une mise à jour (voire une suppression) peut donc partir sur la mauvaise exploitation sans aucune confirmation.
* Correctif proposé : si plusieurs fermes existent, relayer une `MenuRequest` pour demander explicitement laquelle utiliser avant de poursuivre l’exécution ou renvoyer vers validator pour collecte du champ.

## Recommandations prochaines étapes
* Implémenter une matrice d’exigences par intent pour piloter TaskHandler (et rejouer un test régression sur toutes les intentions producteur).
* Redéfinir FARM_CRITICAL_GOALS en deux ensembles (WRITE_REQUIRES_FARM, READ_OPTIONAL) puis ajuster ensure_farm_node pour ne s’exécuter que lorsque le schéma MCP requiert vraiment farm_id.
* Corriger la chaîne MARKET_GET_REQUEST_DETAIL (resolver + _prep_market_get_request_detail) afin que l’identifiant sélectionné soit transmis jusqu’au tool.
* Refactorer _resolve_default_farm pour qu’il respecte user_farms_cache, avec fallback MCP uniquement si le cache est vide ou obsolète.
* Ces chantiers couvrent les failles critiques mentionnées par l’utilisateur ; je peux prioriser leur mise en œuvre ou approfondir d’autres pans (interpréteur producteur, menus AG‑UI, etc.) selon tes besoins.