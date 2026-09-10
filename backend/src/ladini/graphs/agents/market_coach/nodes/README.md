# Nodes MarketCoach — rôles et interactions

Chaque fichier de ce dossier correspond à un nœud LangGraph. Tous les nœuds
suivent la convention suivante :
- reçoivent l’état `MarketAgentState` + `MarketRuntime`;
- retournent un patch d’état partiel (reducers appliqués par le graphe) ;
- ne contiennent pas de logique métier cart/producer (déléguée aux flows).

## Vue d’ensemble du pipeline
1. **input_normalizer.py** — nettoie le message utilisateur (diacritiques,
   numéros, complétion d’identifiants) et hydrate `transaction_payload`.
2. **security_moderation.py** — applique la politique sécurité (anti-spam,
   blocage des commandes interdites, grade entreprise).
3. **validation.py** — vérifie la complétude des champs attendus, construit des
   `missing_fields`, peut déclencher des formulaires AG‑UI.
4. **cognitive.py** — boucle de planification cognitive (progression, plan
   courant, interruptions) avant que l’intent ne soit résolu.
5. **semantic_disambiguation.py** — clarifie les demandes ambiguës via menus
   (Liste de produits, reformulations).
6. **clarification.py** — nœud spécialisé pour demander une confirmation/une
   précision additionnelle avec pédagogie.
7. **form_node.py** — exécute les formulaires dynamiques (drivers generiques).
8. **memory.py** — fusionne `extracted_entities`, résout les sélections UI
   (mapping/index) et maintient les entités stables.
9. **routing.py** — helpers de post‑validator (ex. `make_route_after_validator`).
10. **confirmation_gate.py** — gère l’attente explicite de confirmation (oui/non)
    avant une action risquée.
11. **executor.py** — déclenche les outils MCP (lecture/écriture) selon le plan
    validé et gère les erreurs/outils indisponibles.
12. **ui_engine.py** — convertit un `MenuRequest` (produit par les flows) en
    composant AG‑UI conforme (ListMenu, Confirmation, etc.) et synchronise les
    `available_mapping`.
13. **response_handlers.py** — finalise la réponse utilisateur (SUCCESS,
    ASK_CLARIFICATION, ERROR, etc.), formate le message WhatsApp et pilote la
    `final_response`.

Chaque nœud est indépendant : pour introduire une nouvelle capacité (ex. un
"shipping_gate"), ajouter un fichier ici, le référencer dans
`core/graph_builder.py` et laisser les flows écrire/consommer les champs d’état
appropriés.
