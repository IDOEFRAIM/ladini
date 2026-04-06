# ☁️ AgriConnect - Déploiement & Infrastructure

Ce guide explique comment mettre en production l'architecture AgriConnect sur un serveur DigitalOcean (ou tout serveur Docker compatible).

## 🚀 Vue d'Ensemble

L'infrastructure cible la sobriété et l'efficacité pour un budget initial de **200 $ (crédits gratuits sur 5 mois)**.

### Configuration Standard (4GB RAM)

- **Serveur** : DigitalOcean Droplet Basic (4GB RAM, 2 CPU).
- **Base de Données** : Managed PostgreSQL (1GB RAM) ou self-hosted sur le Droplet.
- **Stockage Objets** : Non critique (fichiers audio temporaires).
- **Traffic** : HTTP/HTTPS via Nginx Proxy Manager ou Traefik.

---

## 📦 Stack Docker Production

Le fichier `docker-compose.production.yml` orchestre tous les services nécessaires :

| Service | Image | Fonction | Port | RAM Est. |
|---------|-------|----------|------|----------|
| **postgres** | `pgvector/pgvector:pg16` | Données + Vecteurs | 5432 | 1GB |
| **redis** | `redis:7-alpine` | Broker + Cache | 6379 | 512MB |
| **api-agent** | `agribot/api:latest` | FastAPI + LangGraph | 8000 | 1GB |
| **worker-voice** | `agribot/worker-voice:latest` | Tâches lourdes (TTS) | - | 512MB |
| **worker-ai** | `agribot/worker-ai:latest` | Réponses complexes | - | 512MB |
| **beat** | `agribot/beat:latest` | Scheduler Cron | - | 256MB |
| **flower** | `mher/flower:latest` | Monitoring Celery | 5555 | 256MB |

**Total RAM Requis : ~4GB**

---

## 🛠️ Guide de Déploiement Rapide (DigitalOcean)

### 1. Prérequis

- [ ] Compte DigitalOcean actif.
- [ ] SSH Key configurée.
- [ ] Noms de domaine pointant vers l'IP du Droplet.

### 2. Initialisation du Droplet

```bash
# Se connecter en SSH
ssh root@votre-ip

# Mettre à jour et installer Docker
apt update && apt upgrade -y
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh

# Installer Docker Compose (déjà inclus souvent, sinon v2 plugin)
```

### 3. Installation AgriConnect

```bash
# Cloner le dépôt
git clone https://github.com/votre-org/Agribot-AI.git
cd Agribot-AI

# Configurer l'environnement
cp .env.example .env
nano .env
# Remplir :
# - DATABASE_URL (postgres://...)
# - AZURE_SPEECH_KEY
# - GROQ_API_KEY
# - TWILIO_AUTH_TOKEN
```

### 4. Lancement

```bash
# Build et démarrage en mode détaché
docker compose -f docker-compose.production.yml up -d --build

# Vérifier les logs
docker compose logs -f api-agent
```

### 5. Initialisation Base de Données

```bash
# Appliquer le schéma SQL (crée tables, extensions, index vectoriels)
docker compose exec -T postgres psql -U agriconnect agriconnect < backend/database/init.sql
```

A ce stade, l'API est accessible sur `http://votre-ip:8000`.

---

## 📈 Scalabilité

### Phase 1 : Monolithe (0 - 5k utilisateurs)
- Tout sur un seul Droplet 4GB.
- Coût : ~24 $/mois (Droplet) + 15 $ (DB gérée optionnelle) = **39 $/mois**.

### Phase 2 : Découplage (5k - 50k utilisateurs)
- **Frontal** : Droplet dédié pour l'API (Load Balancer si nécessaire).
- **Backend** : Droplet dédié pour les Workers (tâches lourdes).
- **Données** : PostgreSQL Managed Cluster (haute dispo).
- Coût : ~150 $/mois.

### Phase 3 : Kubernetes (50k+ utilisateurs)
- Migration vers DOKS (DigitalOcean Kubernetes Service).
- Autoscaling horizontal des pods API.
- Coût : 300-500 $/mois.

---

## 🛡️ Sécurité & Maintenance

### Sauvegardes
- **Base de Données** : Dump quotidien automatique (si managed) ou via script cron (`pg_dump`).
- **Configuration** : .env sécurisé, ne jamais commiter sur Git.

### Monitoring
- **Flower** (`http://ip:5555`) : Suivi des tâches asynchrones en temps réel.
- **Santé Système** : `GET /health` sur l'API retourne l'état de la DB et de Redis.
- **Logs** : Centralisés via Docker (`docker compose logs`).

### Mises à jour
```bash
git pull origin main
docker compose -f docker-compose.production.yml up -d --build
```
Zero-downtime possible avec un reverse proxy load-balancer en amont.
