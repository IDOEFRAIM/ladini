# Runbook — test à petite échelle, COÛT RÉEL, sur intégrations réelles

> Ceci n'est **pas** un script — c'est une procédure manuelle, volontairement
> restreinte. Objectif : vérifier UNE FOIS que le pipeline complet (webhook
> signé → idempotence → Celery → worker → LLM → réponse WhatsApp) fonctionne
> de bout en bout avec de vraies intégrations, sans les risques d'un vrai
> test de charge (des centaines d'appels LLM facturés, des dizaines de vrais
> messages WhatsApp envoyés à des numéros qui n'existent pas ou existent
> vraiment).

---

## ⚠️ AVERTISSEMENT DE COÛT — lire avant de commencer

Chaque itération de cette procédure déclenche :
- **1 appel LLM réel** facturé sur `GROQ_API_KEY` (coût Groq — faible à
  l'unité, mais RÉEL, pas un test unitaire mocké) ;
- potentiellement **1 envoi WhatsApp réel** via `WHATSAPP_CLOUD_API_TOKEN`/
  `TWILIO_AUTH_TOKEN`, si le numéro cible existe et a un template/une fenêtre
  de conversation ouverte (sinon Meta/Twilio répond une erreur — voir
  `_log_failed_status` dans `whatsapp_webhook.py`, ce n'est pas gratuit pour
  autant côté quota API) ;
- de vraies écritures en base (`WorkspaceStore`, idempotence Redis) si la
  cible est une base partagée avec de vraies données — **utilisez un numéro
  de téléphone de test dédié**, jamais un numéro d'utilisateur réel.

**Échelle volontairement bornée : 5 VUs, 1 itération chacun = 5 messages
traités, 5 appels LLM, au plus 5 tentatives d'envoi WhatsApp.** Ne montez
JAMAIS ce chiffre sans relire ce document et sans un accord explicite sur le
budget consommé.

---

## Pré-requis

1. Confirmer que la cible (`TARGET_URL`) tourne avec des identifiants
   **sandbox/test** :
   - `GROQ_API_KEY` : idéalement une clé de dev avec budget plafonné (Groq ne
     propose pas de mode "sandbox" dédié — vérifier le budget/l'alerte de
     facturation du compte AVANT).
   - `MESSAGING_PROVIDER=whatsapp_cloud` : utiliser un numéro de test du
     WhatsApp Business Sandbox (Meta for Developers → votre app → WhatsApp →
     "API Setup" fournit un numéro de test + une liste de destinataires
     autorisés, gratuits, à ajouter explicitement).
   - `MESSAGING_PROVIDER=twilio` : utiliser le **Twilio WhatsApp Sandbox**
     (`whatsapp:+14155238886` par défaut) — messages gratuits, uniquement
     vers des numéros ayant rejoint le sandbox (`join <code>`).
2. Avoir le vrai `TWILIO_AUTH_TOKEN` ou `WHATSAPP_APP_SECRET` de CETTE cible
   sandbox sous la main (jamais celui de prod).
3. Avoir accès à Flower (`http://127.0.0.1:5555` ou tunnel SSH) pour observer
   les 5 tâches en direct.

---

## Procédure

```bash
# 1. Ouvrir Flower dans un onglet pour observer les 5 tâches en direct.

# 2. Lancer EXACTEMENT 5 VUs, 1 itération chacun (--iterations force le
#    comportement, indépendamment de tout SCENARIO/DURATION du script) :
TARGET_URL=https://votre-sandbox.example.com \
PROVIDER=whatsapp_cloud \
SIGNING_SECRET="<WHATSAPP_APP_SECRET de la cible sandbox>" \
k6 run --vus 5 --iterations 5 load-tests/webhook-scenario.js

# 3. Vérifier dans Flower : 5 tâches `process_agent_task` REÇUES puis
#    TERMINÉES (SUCCESS), pas bloquées en PENDING.

# 4. Vérifier les logs worker (docker compose logs worker --tail 100) :
#    pas d'exception, `ladini_llm_calls_total{status="success"}` incrémenté
#    de 5 (curl http://.../metrics | grep ladini_llm_calls_total avant/après).

# 5. Si un vrai envoi WhatsApp était attendu (numéro de test dans les
#    destinataires autorisés) : confirmer la réception sur le téléphone de
#    test.
```

---

## Après le test

- Documenter le résultat (succès/échec, latence observée par tâche) dans le
  ticket/PR de ce chantier — ce runbook ne remplace pas
  `load-tests/webhook-scenario.js`/`burst-scenario.js` pour la mesure de
  capacité, il ne fait que PROUVER que le pipeline signé fonctionne de bout
  en bout avant de faire confiance aux résultats "rejeté avant enqueue" des
  scripts de charge par défaut.
- Ne jamais laisser `SIGNING_SECRET` dans l'historique shell/CI en clair —
  passer par une variable d'environnement chargée depuis un secret manager,
  jamais committée.
