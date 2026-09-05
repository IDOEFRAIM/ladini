# MCP TOOL EXPOSURE POLICY

> **Convention d'architecture officielle.** Toute évolution de la surface
> MCP doit s'y conformer.

## La règle

```
Une méthode publique de AgriDatabaseService N'EST PAS un outil MCP,
sauf si son nom figure explicitement dans MCP_EXPOSED_TOOLS.

Tout outil exposé DOIT déclarer un scope dans TOOL_SCOPE_MAP.

Une méthode système (cron, IPN, worker, réconciliation) NE DOIT PAS
être exposée : ses appelants passent directement par le service.

Un outil inconnu ou non enregistré est REFUSÉ (fail-closed).
```

## La chaîne d'autorité

```
MCP_EXPOSED_TOOLS          →   TOOL_SCOPE_MAP        →   runtime
infrastructure/mcp/            infrastructure/mcp/       call_tool()
exposure.py                    security.py

« Cet outil existe-t-il ? »    « Qui a le droit ? »      « fail-closed »
```

Les deux listes ont des rôles **distincts** et doivent rester cohérentes :
exposer sans scope rend l'outil inutilisable ; déclarer un scope sans
exposer est trompeur. Un test vérifie les deux sens.

## Ajouter un outil

1. **La méthode doit contrôler la propriété** : résoudre l'acteur
   (`get_buyer_profile` / `get_producer_profile` depuis le téléphone
   épinglé) **et** filtrer la ressource par cet acteur. Voir
   `tests/architecture/test_order_mutations_require_ownership.py`.
2. **Ne jamais accepter un statut arbitraire** en paramètre sans liste
   blanche (cf. `cancel_preorder_draft`).
3. Ajouter le nom dans `MCP_EXPOSED_TOOLS`, dans le bon groupe.
4. Déclarer un scope dans `TOOL_SCOPE_MAP`.

Les quatre étapes sont nécessaires. Il n'existe aucun chemin par défaut.

## Ne PAS exposer

| Catégorie | Exemples | Pourquoi |
|---|---|---|
| **Système** | `mark_escrow_paid`, `expire_pending_payments`, `ensure_performance_indexes`, `check_and_expire_auctions` | appelées en direct par un cron/IPN/worker ; sans acteur par nature |
| **Interne** | `get_buyer_profile`, `get_producer_profile`, `guess_category`, `resolve_sub_category`, `normalize_unit` | helpers de résolution utilisés *dans* la couche service |
| **Dangereux** | `update_order_status` | écrit statut et paiement arbitraires, sans propriétaire |
| **Mort / hérité** | `finalize_multi_order`, `update_production_visibility`, `toggle_product_availability` | `finalize_multi_order` recréerait une commande multi-producteurs |

**« Public » ≠ « outil MCP ».** Une méthode Python publique non exposée
reste parfaitement appelable depuis le code : c'est le mode d'appel normal
des workers et des services.

## Pourquoi cette politique existe

Le modèle précédent exposait **tout** par introspection et refusait
*certains* outils via l'absence de scope (opt-out / deny-by-omission).
Quatre failles en sont issues, toutes de la même racine :

| Faille | Ce qu'elle permettait |
|---|---|
| `update_order_status` | marquer n'importe quelle commande `PAID`/`COMPLETED`/`CANCELLED` |
| `mark_escrow_paid` | déclarer un paiement reçu sans le fournisseur de paiement |
| `expire_pending_payments` | annuler en masse les commandes en attente |
| `cancel_preorder_draft(target_status)` | faire passer un brouillon à `COMPLETED` sans paiement ni stock |

Aucune n'était un bug isolé : c'était le même défaut structurel, quatre
fois. L'allow-list supprime la classe entière.

## Tests qui font respecter la politique

| Test | Garantit |
|---|---|
| `test_a_new_public_method_is_not_exposed` | une méthode ajoutée demain n'est **pas** un outil |
| `test_no_declared_tool_is_missing_from_the_service` | pas de nom fantôme dans la liste |
| `test_every_exposed_tool_has_a_scope` | cohérence exposition ↔ autorisation |
| `test_every_goal_tool_that_exists_is_still_exposed` | aucune capacité produit perdue |
| `test_every_literally_invoked_tool_is_still_exposed` | aucun appel runtime cassé |
| `TestDangerousAndSystemToolsAreInvisible` | les 4 failles restent fermées |
| `test_the_allowlist_actually_shrank_the_surface` | garde-fou : le filtrage est bien actif |
