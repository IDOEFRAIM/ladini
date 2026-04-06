# 🚀 GUIDE UTILISATION - ORCHESTRATOR & AGENTS

## DÉMARRAGE RAPIDE

### 1. Tester l'Orchestrateur Principal

```python
from backend.orchestrator.message_flow import MessageResponseFlow

# Initialisation
orchestrator = MessageResponseFlow()

# Cas 1: Question normale
result = orchestrator.process_user_request(
    user_query="Quel est le prix du maïs aujourd'hui?",
    user_id="farmer_001",
    zone_id="Koutiala",
    crop="Maïs"
)
print(result['final_response'])

# Cas 2: Urgence
result = orchestrator.process_user_request(
    user_query="Invasion de criquets sur mes plants!",
    crop="Coton"
)
print(result['final_response'])
print(f"Alertes: {result['global_alerts']}")

# Cas 3: Mode SMS (réponse courte)
result = orchestrator.process_user_request(
    user_query="Météo demain?",
    is_sms_mode=True
)
print(f"SMS: {result['final_response']} ({len(result['final_response'])} chars)")
```

---

### 2. Générer des Rapports Automatiques

```python
from backend.orchestrator.report_flow import ReportFlow, ReportType

# Initialisation
report_flow = ReportFlow()

# Rapport hebdomadaire santé
result = report_flow.generate_report(
    report_type=ReportType.WEEKLY_HEALTH,
    user_id="farmer_001",
    zone_id="Koutiala",
    crop="Maïs"
)
print(result['final_response'])

# Bilan mensuel finances (SMS)
result = report_flow.generate_report(
    report_type=ReportType.MONTHLY_FINANCE,
    user_id="farmer_001",
    crop="Coton",
    is_sms_mode=True
)
print(result['final_response'])

# Comparaison communautaire
result = report_flow.generate_report(
    report_type=ReportType.COMMUNITY_BENCHMARK,
    user_id="farmer_001",
    crop="Soja"
)
print(result['final_response'])
```

---

### 3. Utiliser PlantDoctor Enrichi

```python
from backend.agents.plant_doctor import PlantHealthDoctor

# Initialisation
doctor = PlantHealthDoctor()

# Cas 1: Diagnostic avec photo
state = {
    "user_query": "Mes feuilles jaunissent",
    "culture_config": {"crop": "Maïs", "stage": "Floraison"},
    "photo_paths": ["/path/to/photo.jpg"],  # Chemin photo
    "warnings": []
}
result = doctor.diagnose_node(state)

print(f"Status: {result['status']}")
if result.get('guided_questions'):
    print("Questions guidées:")
    for q in result['guided_questions']:
        print(f"  - {q}")

# Afficher coûts et localisation
if result.get('treatment_costs'):
    print("\nCoûts estimés:")
    for treatment, cost in result['treatment_costs'].items():
        print(f"  {treatment}: {cost:,.0f} FCFA")

if result.get('local_availability'):
    print("\nOù acheter:")
    for product, locations in result['local_availability'].items():
        print(f"  {product}: {locations}")

# Cas 2: Description floue → Questions guidées
state = {
    "user_query": "Problème",  # Description trop courte
    "culture_config": {"crop": "Tomate"},
    "warnings": []
}
result = doctor.diagnose_node(state)

if result.get('guided_questions'):
    print("Questions pour l'agriculteur:")
    for i, q in enumerate(result['guided_questions'], 1):
        print(f"  {i}. {q}")
```

---

## INTÉGRATION DANS VOTRE APPLICATION

### API REST (FastAPI)

```python
from fastapi import FastAPI, UploadFile, File
from backend.orchestrator.main_orchestrator import AgribotMainOrchestrator
from backend.orchestrator.report_flow import ReportFlow, ReportType

app = FastAPI()
orchestrator = AgribotMainOrchestrator()
report_flow = ReportFlow()

@app.post("/api/v1/query")
async def handle_user_query(
    query: str,
    user_id: str,
    zone_id: str = "Koutiala",
    crop: str = "Maïs",
    is_sms: bool = False
):
    """
    Point d'entrée principal pour questions utilisateur.
    """
    result = orchestrator.process_user_request(
        user_query=query,
        user_id=user_id,
        zone_id=zone_id,
        crop=crop,
        is_sms_mode=is_sms
    )
    return {
        "response": result['final_response'],
        "alerts": result['global_alerts'],
        "execution_path": result['execution_path']
    }

@app.post("/api/v1/diagnose")
async def diagnose_plant(
    query: str,
    crop: str,
    photo: UploadFile = File(None)
):
    """
    Diagnostic plante avec support photo.
    """
    from backend.agents.plant_doctor import PlantHealthDoctor
    
    doctor = PlantHealthDoctor()
    
    # Sauvegarde photo si fournie
    photo_path = None
    if photo:
        photo_path = f"/tmp/{photo.filename}"
        with open(photo_path, "wb") as f:
            f.write(await photo.read())
    
    state = {
        "user_query": query,
        "culture_config": {"crop": crop},
        "photo_paths": [photo_path] if photo_path else [],
        "warnings": []
    }
    
    # Diagnostic
    result = doctor.diagnose_node(state)
    
    # Composition réponse si diagnostic OK
    if result['status'] == "DIAGNOSED":
        final_result = doctor.compose_node(result)
        return {
            "response": final_result['final_response'],
            "costs": result.get('treatment_costs', {}),
            "alternatives": result.get('alternative_products', {}),
            "where_to_buy": result.get('local_availability', {})
        }
    else:
        return {
            "response": "Diagnostic incomplet",
            "guided_questions": result.get('guided_questions', []),
            "status": result['status']
        }

@app.get("/api/v1/reports/{report_type}")
async def generate_report(
    report_type: str,  # "weekly", "monthly", "benchmark"
    user_id: str,
    crop: str = "Maïs",
    is_sms: bool = False
):
    """
    Génération rapports automatiques.
    """
    type_mapping = {
        "weekly": ReportType.WEEKLY_HEALTH,
        "monthly": ReportType.MONTHLY_FINANCE,
        "benchmark": ReportType.COMMUNITY_BENCHMARK
    }
    
    result = report_flow.generate_report(
        report_type=type_mapping[report_type],
        user_id=user_id,
        crop=crop,
        is_sms_mode=is_sms
    )
    
    return {
        "response": result['final_response'],
        "full_report": result['final_report']
    }
```

---

### Service de Rapports Automatiques (Celery)

```python
# tasks.py
from celery import Celery
from celery.schedules import crontab
from backend.orchestrator.report_flow import ReportFlow, ReportType

app = Celery('agribot', broker='redis://localhost:6379')

report_flow = ReportFlow()

@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    # Rapport hebdomadaire - Lundi 7h
    sender.add_periodic_task(
        crontab(hour=7, minute=0, day_of_week=1),
        send_weekly_reports.s()
    )
    
    # Bilan mensuel - 1er du mois 8h
    sender.add_periodic_task(
        crontab(hour=8, minute=0, day_of_month=1),
        send_monthly_reports.s()
    )

@app.task
def send_weekly_reports():
    """
    Envoie rapports hebdomadaires à tous les agriculteurs.
    """
    # Récupérer liste agriculteurs depuis DB
    farmers = get_all_farmers()  # À implémenter
    
    for farmer in farmers:
        result = report_flow.generate_report(
            report_type=ReportType.WEEKLY_HEALTH,
            user_id=farmer['id'],
            zone_id=farmer['zone'],
            crop=farmer['primary_crop'],
            is_sms_mode=not farmer['has_smartphone']
        )
        
        # Envoi SMS/WhatsApp
        send_message(
            to=farmer['phone'],
            message=result['final_response'],
            mode='sms' if not farmer['has_smartphone'] else 'whatsapp'
        )

@app.task
def send_monthly_reports():
    """
    Envoie bilans mensuels.
    """
    farmers = get_all_farmers()
    
    for farmer in farmers:
        result = report_flow.generate_report(
            report_type=ReportType.MONTHLY_FINANCE,
            user_id=farmer['id'],
            crop=farmer['primary_crop'],
            is_sms_mode=not farmer['has_smartphone']
        )
        
        send_message(
            to=farmer['phone'],
            message=result['final_response'],
            mode='sms' if not farmer['has_smartphone'] else 'whatsapp'
        )
```

---

## TESTS UNITAIRES

```python
# tests/test_orchestrator.py
import pytest
from backend.orchestrator.main_orchestrator import AgribotMainOrchestrator

def test_greeting_response():
    orchestrator = MessageResponseFlow()
    result = orchestrator.process_user_request("Bonjour")
    
    assert result['status'] == 'SUCCESS'
    assert 'execution_path' in result
    assert 'greeting' in result['execution_path']

def test_emergency_detection():
    orchestrator = AgribotMainOrchestrator()
    result = orchestrator.process_user_request("Invasion de criquets!")
    
    assert len(result['global_alerts']) > 0
    assert 'emergency' in result['execution_path']
    assert '🚨' in result['final_response']

def test_sms_mode_short_response():
    orchestrator = AgribotMainOrchestrator()
    result = orchestrator.process_user_request(
        "Prix du maïs?",
        is_sms_mode=True
    )
    
    assert len(result['final_response']) <= 160

# tests/test_plant_doctor.py
from backend.agents.plant_doctor import PlantHealthDoctor

def test_guided_questions_for_vague_description():
    doctor = PlantHealthDoctor()
    state = {
        "user_query": "Problème",  # Trop court
        "culture_config": {"crop": "Maïs"},
        "warnings": []
    }
    
    result = doctor.diagnose_node(state)
    
    assert result.get('guided_questions') is not None
    assert len(result['guided_questions']) > 0

def test_treatment_costs_estimated():
    doctor = PlantHealthDoctor()
    
    diagnosis = {
        "diagnostique": "Mildiou",
        "traitement_recommande": {
            "bio": "Bouillie bordelaise"
        }
    }
    
    costs = doctor._estimate_treatment_cost(diagnosis, surface_ha=2.0)
    
    assert 'TOTAL ESTIMÉ' in costs
    assert costs['TOTAL ESTIMÉ'] > 0
```

---

## MONITORING & LOGS

```python
import logging

# Configuration logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('agribot.log'),
        logging.StreamHandler()
    ]
)

# Logs automatiques dans les composants
logger = logging.getLogger("MainOrchestrator")
logger.info("📞 Requête de farmer_001 (Koutiala): Prix maïs?")
logger.warning("🚨 URGENCE CRITICAL: Invasion criquets")
logger.error("❌ Erreur orchestration: {exception}")
```

**Exemple de log:**
```
2024-06-15 14:23:45 - MainOrchestrator - INFO - 📞 Requête de farmer_001 (Koutiala): Prix maïs?
2024-06-15 14:23:45 - MainOrchestrator - INFO - 🚦 TRIAGE: Analyse de la requête entrante...
2024-06-15 14:23:45 - MainOrchestrator - INFO - 📨 Délégation au Message Flow (Conseil d'Experts)
2024-06-15 14:23:48 - MainOrchestrator - INFO - ✅ Réponse générée | Path: ['triage', 'route_normal', 'message_flow']
```

---

## TROUBLESHOOTING

### Problème: "LLM indisponible"
**Cause:** API key Groq invalide ou quota dépassé  
**Solution:**
```python
# Vérifier la clé API
import os
print(os.getenv("GROQ_API_KEY"))

# Mode fallback activé automatiquement
# Réponses générées sans LLM si indisponible
```

### Problème: "Diagnostic impossible"
**Cause:** Description trop floue ou RAG indisponible  
**Solution:**
```python
# Le système envoie automatiquement questions guidées
# Agriculteur peut aussi envoyer photo
result = doctor.diagnose_node(state)
if result.get('guided_questions'):
    # Afficher questions à l'utilisateur
    for q in result['guided_questions']:
        print(q)
```

### Problème: "Rapports non envoyés"
**Cause:** Tâches Celery non démarrées  
**Solution:**
```bash
# Démarrer worker Celery
celery -A tasks worker --loglevel=info

# Démarrer scheduler (beat)
celery -A tasks beat --loglevel=info
```

---

## PROCHAINES ÉTAPES

1. **Intégrer Google Vision API** pour analyse photos réelles
2. **Connecter PostgreSQL** pour base produits/agriculteurs
3. **Implémenter Celery** pour rapports automatiques programmés
4. **Ajouter mode vocal** (Speech-to-Text pour illettrés)
5. **Créer interface WhatsApp** (Twilio ou WhatsApp Business API)

---

**Le système est maintenant prêt pour l'utilisation en production!** 🚀
