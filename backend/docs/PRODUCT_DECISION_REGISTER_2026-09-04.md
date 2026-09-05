# PRODUCT DECISION REGISTER — 2026-09-04 (Phase 5)

Les 4 décisions restées ouvertes après les phases précédentes, avec leurs
preuves, leurs options et leur statut. **Aucune décision n'est prise par
défaut technique** : chaque ligne dit explicitement si le dépôt tranche
(`repository-determined`) ou si un arbitrage humain reste requis.

| Decision | Current state | Evidence | Options | Recommendation | Decision status |
|---|---|---|---|---|---|
| **#1 Annulation/rejet producteur d'une commande `CONFIRMED`** | Aucune action producteur après `CONFIRMED` : la seule issue est de demander à l'acheteur d'annuler (contournement hors-app) | `cancel_pending_order` (buyer) couvre `PENDING`/`CONFIRMED`→`CANCELLED` avec recrédit de stock, `Order.cancellation_role` déjà en base et déjà rempli (`"BUYER"`), résolution de propriété dual-origine déjà écrite (F1), paiement à la livraison ⇒ rien à rembourser | (a) symétrie stricte du chemin acheteur ; (b) demande soumise à l'accord de l'acheteur ; (c) fenêtre de refus courte après notification | **(a)** — seule option réalisable sans inventer de statut ni de cron, ce que ce mandat interdit explicitement (§5 « ne crée pas un nouveau statut sans nécessité », §19 « pas de refactor ») | **CLOSED — implémentée** |
| **#2 `SALES_ACCEPT_CONTRACT` / sémantique du « contrat »** | Goal déprécié (Phase 4), handler intact, `commit_staged_transaction` inexistant | Recherche exhaustive : aucune entité `Contract`/`StagedTransaction`, aucune table, aucune référence frontend/doc/eval. `SalesService.accept_contract` retourne ce tool_id inconditionnellement. Seules traces : 2 tests de routage de résolveur | (a) doublon d'un mécanisme existant (négociation/enchère) ⇒ supprimer ; (b) vraie contractualisation à terme ⇒ nouveau domaine complet ; (c) reliquat d'un flux « staging » remplacé par Drafts+CAS ⇒ supprimer | **Aucune** — le dépôt ne permet pas de répondre aux 8 questions métier (qui crée, qui accepte, quelle entité, quels statuts, quel état terminal, qui est notifié, rapport à `Order`) | **OPEN — PRODUCT_DECISION** |
| **#3 `PROFILE_SWITCH_ROLE` / bascule de rôle** | Goal déprécié (Phase 4), écrit une ligne `agent_actions` inerte | La refonte double-rôle est **définitive** et documentée dans 6 fichiers cœur (`graph_builder`, `router` ×2, `intent`, `routing`) : graphe unifié, aucun rôle de graphe, tout utilisateur vend ET achète message par message. La ligne écrite n'a **aucun consommateur** (prouvé indépendamment : `tests/evals/blocked/PROFILE_SWITCH_ROLE.md`). `create_agent_action` n'existe pas comme outil | (a) déprécier — le modèle double-rôle rend la bascule inutile ; (b) construire une vraie bascule (rôle de session, permissions) | **(a)** — `repository-determined` : le rôle est déjà implicite par action, une bascule explicite n'ajoute rien | **CLOSED — déprécié** |
| **#4 Exposition du ledger STOCK** | Écritures + lectures à identifiant dépréciées (Phase 4) ; `STOCK_REGISTER_HARVEST` et `STOCK_GET_SUMMARY` restent exposés | `Product.quantity_for_sale` est **la seule autorité** pour la vente : débité au checkout (`buyer.py:662`, `2074`), recrédité à l'annulation (`917`), écrit par escrow/création/édition/retrait. Les méthodes `Stock` (`add_stock`, `adjust_stock`, `remove_stock`, `delete_stock`, `add_stock_movement`) ne sont appelées par **aucun** chemin transactionnel. « Stock insuffisant » côté acheteur lit `product.quantity_for_sale`, jamais la table `Stock` | (a) garder le ledger hors catalogue ; (b) l'exposer (corriger 4 `tool_name` dérivés + 5 entrées passthrough) ; (c) le relier aux ventes (sémantique de mouvement à définir) | **(a)** — règle par défaut du mandat (§9) + preuve que `Stock` n'est pas autoritatif : l'exposer créerait une capacité sans effet sur ce qui est vendu | **CLOSED par défaut — sous-question (b/c) ouverte si le besoin apparaît** |

---

## Détail #1 — ce qui est déterminé par le dépôt vs ce qui reste un choix

| Aspect | Statut | Source |
|---|---|---|
| Statut cible = `CANCELLED` | **repository-determined** | statut existant, déjà écrit par le chemin acheteur, déjà libellé dans les deux dashboards |
| Statuts annulables = `CONFIRMED` uniquement | **repository-determined** | `DRAFT` a son propre chemin (`cancel_preorder_draft`) ; `COMPLETED`/`CANCELLED` sont terminaux |
| Aucun remboursement | **repository-determined** | paiement à la livraison : rien n'a été encaissé avant |
| Recrédit du stock | **repository-determined** | `resolve_stock_debit` sur `OrderItem` (préorder) ; no-op pour une commande RFQ, qui n'a jamais d'`OrderItem` |
| Propriété producteur | **repository-determined** | même résolution dual-origine que `confirm_delivery_and_payment` |
| Traçabilité | **repository-determined** | `OrderStatusHistory` + `Order.cancellation_role="PRODUCER"` (colonne existante) |
| Notification acheteur | **repository-determined** | miroir exact de `ORDER_CANCELLED_BY_BUYER_PRODUCER` |
| Motif libre, journalisé | **repository-determined** | le chemin acheteur journalise `[CancelReason] …` dans `delivery_desc` — pas de taxonomie inventée (mandat §3) |
| **Limite anti-abus producteur** | **PRODUCT DECISION — non implémentée** | `MAX_CANCELLATIONS=3` existe mais est filtré sur `Order.buyer_id` + rôle `BUYER`. Aucune règle producteur nulle part. **Aucun compteur ni blocage n'a été ajouté** : inventer une sanction serait un choix métier arbitraire |
| **Réouverture d'une enchère après annulation d'une commande RFQ** | **PRODUCT DECISION — non implémentée** | le chemin acheteur ne rouvre pas l'enchère non plus ; la symétrie est donc conservée. Rouvrir impliquerait un mécanisme neuf (repêchage des perdants) |

### Pourquoi les options (b) et (c) ont été écartées — et non « non choisies »

* **(b) demande soumise à l'accord de l'acheteur** exigerait un état intermédiaire (`CANCELLATION_REQUESTED`) et un cron de délai. Le mandat interdit explicitement de créer un statut sans nécessité et de lancer un nouveau mécanisme.
* **(c) fenêtre de refus courte** exigerait une notion de délai de livraison contractuel, qui n'existe nulle part dans le modèle (`Order` n'a pas d'échéance opposable).

Ces deux options restent ouvertes comme évolutions produit ; elles ne sont
pas réalisables dans le périmètre de cette phase.
