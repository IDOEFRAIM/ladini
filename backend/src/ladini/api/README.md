# 🚀 Ladini - Module API & Worker

Ce dossier contient le point d'entrée de l'application **Ladini MarketCoach**. L'architecture repose sur **FastAPI** pour l'exposition des endpoints et **Celery + Redis** pour le traitement asynchrone des agents IA (Orchestrateur).

---

## 📂 Structure du Dossier

* `routes/` : Contient les endpoints de l'API.
    * `market.py` : Routes pour exposer les agents (Producteur / Acheteur) via HTTP classique (mode polling/statut).
    * `twilio_webhook.py` : Endpoint webhook sécurisé qui réceptionne les messages WhatsApp de Twilio.
* `celery_app.py` : Configuration de l'application Celery (gestion des files d'attente, pré-chargement, expiration).
* `tasks.py` : Tâches asynchrones Celery exécutées par le worker (fait le lien avec l'Orchestrator).
* `dependencies.py` : Dépendances FastAPI (graphes, sessions, etc.).
* `schemas.py` : Modèles de données Pydantic (validation des requêtes et réponses).
* `server.py` : Point d'entrée pour le serveur de développement Uvicorn.
* `main.py` : Initialisation de FastAPI, configuration des middlewares (CORS) et inclusion des routes.

---

## 🔄 Flux d'exécution (WhatsApp / Webhook)

1. **Réception :** Twilio envoie un `POST` sur `/api/webhook/twilio`.
2. **Sécurité :** La dépendance `verify_twilio_signature` intercepte la requête pour valider l'authenticité de Twilio.
3. **Dispatch :** L'API extrait le message, identifie l'utilisateur via le `WorkspaceStore` et pousse la tâche dans **Redis** via `process_agent_task.delay()`.
4. **Réponse rapide :** L'API retourne immédiatement une réponse `200 OK` à Twilio (évite les timeouts).
5. **Traitement :** Le worker Celery récupère la tâche, exécute l'asynchronisme de l'Orchestrateur IA, puis utilise l'API Twilio pour renvoyer la réponse finale sur le WhatsApp de l'agriculteur.

---

## 🛠️ Commandes Utiles

### 1. Lancer l'API en mode développement
```bash
python -m ladini.api.server