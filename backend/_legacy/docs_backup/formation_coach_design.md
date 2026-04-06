**FormationCoach — Design, Migrations, and Examples**

Résumé
- Objectif: transformer `FormationCoach` en assistant expert agricole utilisant prioritairement des règles métier pour le diagnostic et limitant l'usage du LLM à la génération textuelle concise.

Migrations ajoutées
- `backend/src/agriconnect/database/migration_formationcoach_20260318.sql` — ajoute les tables:
  - `crop_tech_sheets` (fiches techniques)
  - `diagnostics` (historique de diagnostics)
  - `action_plans` (plans d'action liés)
  - `user_memories` (mémoires utilisateur)
  - `formation_feedback` (retours utilisateurs)

Principes d'architecture
- Règles métier = premier niveau (détecter symptômes, produire plan). LLM = second niveau (formatage, reformulation, explications courtes).
- Persistance: diagnostics et plans enregistrés dans la base existante (migrations idempotentes).
- Compatibilité: scripts de migration sont non-destructifs et prévus pour coexister avec le schéma existant (IF NOT EXISTS / ALTER TABLE IF NOT EXISTS).

Recommandations production
- Cache: mettre en cache `crop_tech_sheets` en mémoire (Redis or local TTL cache) pour éviter requêtes répétées.
- Robustesse: toutes les écritures DB sont best-effort; en cas d'échec, retourner l'ID null mais poursuivre la réponse.
- Fallback LLM: si LLM indisponible, renvoyer un texte templatisé.
- Logs: journaliser diagnostic, règles appliquées, et résultat LLM (hash) pour audit.
- Sécurité: utiliser le `Shield` pour contrôler l'accès aux outils RAG / DB quand en production.

Exemples de code d'utilisation (extraits)

1) Lire et mettre à jour le profil utilisateur (via `mcp_context` si disponible)

```python
# read
profile = agent.mcp_context.read_user_context(user_id)

# update
profile['last_visit'] = '2026-03-18'
agent.mcp_context.write_user_context(user_id, profile)
```

2) Lire une fiche technique (extrait de `FormationCoach._get_crop_sheet`)

```python
sheet = agent._get_crop_sheet('Maïs')
if sheet:
    print(sheet['recommendations'])
```

3) Insérer un diagnostic et plan d'action (extraits de helpers)

```python
diag = agent._diagnose_from_query(query, profile, context)
diag_id = agent._save_diagnostic(user_id, crop, zone_id, query, diag['diagnosis'], diag['evidence'], diag['severity'], diag['rules_used'])
plan = agent._build_action_plan_from_diag(diag, sheet)
plan_id = agent._save_action_plan(diag_id, plan)
```

4) Générer la réponse JSON structurée (renvoyée par `compose_node`)

```json
{
  "diagnostic": "Carence en azote détectée",
  "actions_immediates": ["Appliquer engrais azoté selon dosage recommandé"],
  "actions_7_jours": ["Surveiller jaunissement", "Vérifier irrigation"],
  "risks": ["Propagation maladie si humidité élevée"],
  "sources": ["Fiche technique Maïs - Guide local"],
  "diagnostic_id": "...",
  "action_plan_id": "...",
  "final_text": "Court résumé textuel formaté par LLM"
}
```

Notes pour l'intégration CI/CD
- Ajouter `migration_formationcoach_20260318.sql` à la séquence exécutée par `apply_sql_migrations.py`.
- Ajouter tests unitaires couvrant règles métier (ex: input symptom -> expected diagnosis) et tests d'intégration pour insertions DB (use test DB).
