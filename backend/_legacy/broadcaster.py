"""
Agent Broadcaster - Diffuseur de Notifications Push
Lit les alertes non traitées et envoie des notifications ciblées
via WhatsApp, Telegram, SMS.
"""

import logging
import asyncio
from datetime import datetime
from typing import List, Dict, Any
import os

# WhatsApp Business API (via Twilio ou official Cloud API)
from twilio.rest import Client as TwilioClient

# TTS (Text-to-Speech) pour génération audio
from gtts import gTTS
import requests

logger = logging.getLogger("BroadcasterAgent")


class BroadcasterAgent:
    """
    Agent de diffusion proactive.
    
    Responsabilités :
    1. Lire les alertes non traitées (processed=False)
    2. Identifier les utilisateurs cibles (zone + culture)
    3. Générer l'audio TTS dans la langue de l'utilisateur
    4. Envoyer via WhatsApp/Telegram
    5. Marquer l'alerte comme processed=True
    """
    
    def __init__(self, db_client):
        self.db = db_client
        
        # Configuration Twilio (WhatsApp)
        self.twilio_client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )
        self.twilio_whatsapp_number = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+22601479800")
        
        # Configuration Azure Speech (alternative TTS premium)
        self.use_azure_tts = os.getenv("USE_AZURE_TTS", "false").lower() == "true"
        self.azure_speech_key = os.getenv("AZURE_SPEECH_KEY")
        self.azure_region = os.getenv("AZURE_REGION", "westeurope")
    
    async def run_broadcast_cycle(self):
        """
        Cycle de diffusion : traiter toutes les alertes en attente.
        À exécuter toutes les 5-10 minutes.
        """
        logger.info("📢 BroadcasterAgent : Démarrage cycle de diffusion")
        
        try:
            # Récupérer alertes non traitées
            pending_alerts = await self.db.alert.find_many(
                where={"processed": False},
                include={"zone": True},
                order_by={"createdAt": "asc"},
                take=50  # Limiter à 50 par batch
            )
            
            if not pending_alerts:
                logger.info("✅ Aucune alerte en attente")
                return
            
            logger.info(f"📨 {len(pending_alerts)} alerte(s) à diffuser")
            
            for alert in pending_alerts:
                await self._process_alert(alert)
            
            logger.info("✅ Cycle de diffusion terminé")
            
        except Exception as e:
            logger.error(f"❌ Erreur cycle diffusion : {e}", exc_info=True)
    
    async def _process_alert(self, alert):
        """
        Traite une alerte : trouve les destinataires et envoie.
        """
        try:
            # 1. Identifier les utilisateurs cibles
            target_users = await self._find_target_users(alert)
            
            if not target_users:
                logger.warning(f"⚠️ Alerte {alert.id} : Aucun utilisateur ciblé")
                await self._mark_as_processed(alert, sent_count=0)
                return
            
            logger.info(f"🎯 {len(target_users)} utilisateur(s) ciblé(s) pour alerte '{alert.title}'")
            
            # 2. Générer les audios TTS par langue
            audio_files = await self._generate_audio_for_languages(alert, target_users)
            
            # 3. Envoyer les notifications
            sent_count = 0
            for user in target_users:
                success = await self._send_notification(user, alert, audio_files.get(user.language))
                if success:
                    sent_count += 1
            
            # 4. Marquer l'alerte comme traitée
            await self._mark_as_processed(alert, sent_count=sent_count)
            
            logger.info(f"✅ Alerte {alert.id} traitée : {sent_count}/{len(target_users)} envois")
            
        except Exception as e:
            logger.error(f"❌ Erreur traitement alerte {alert.id}: {e}")
    
    async def _find_target_users(self, alert) -> List:
        """
        Trouve les utilisateurs à notifier selon le ciblage de l'alerte.
        """
        filters = {
            "onboarded": True,  # Uniquement utilisateurs actifs
        }
        
        # Filtre géographique
        if alert.zoneId:
            filters["zoneId"] = alert.zoneId
        
        # Filtre par type de notification
        if alert.type == "WEATHER":
            filters["notify_weather"] = True
        elif alert.type == "PEST":
            filters["notify_pest"] = True
        elif alert.type == "MARKET_OPP":
            filters["notify_market"] = True
        
        # Si alerte spécifique à une culture
        if alert.target_crop:
            # Récupérer uniquement les utilisateurs qui cultivent cette culture
            users = await self.db.user.find_many(
                where={
                    **filters,
                    "crops": {
                        "some": {
                            "crop_name": alert.target_crop
                        }
                    }
                }
            )
        else:
            # Alerte générale pour toute la zone
            users = await self.db.user.find_many(where=filters)
        
        return users
    
    async def _generate_audio_for_languages(self, alert, users: List) -> Dict[str, str]:
        """
        Génère les fichiers audio TTS pour chaque langue détectée.
        
        Returns:
            Dict[langue, url_audio]
        """
        languages = set(user.language for user in users)
        audio_files = {}
        
        for lang in languages:
            try:
                audio_url = await self._generate_tts(alert.message, lang)
                audio_files[lang] = audio_url
                logger.info(f"🔊 Audio TTS généré : {lang} → {audio_url}")
            except Exception as e:
                logger.error(f"❌ Erreur TTS pour langue {lang}: {e}")
        
        # Mettre à jour l'alerte avec l'URL audio principale (français par défaut)
        if "fr" in audio_files:
            await self.db.alert.update(
                where={"id": alert.id},
                data={"voice_url": audio_files["fr"]}
            )
        
        return audio_files
    
    async def _generate_tts(self, text: str, language: str) -> str:
        """
        Génère un fichier audio TTS et le stocke.
        
        Options :
        1. gTTS (gratuit, basique)
        2. Azure Speech (premium, GitHub Education)
        3. Google Cloud TTS
        """
        if self.use_azure_tts:
            return await self._azure_tts(text, language)
        else:
            return await self._gtts_simple(text, language)
    
    async def _gtts_simple(self, text: str, language: str) -> str:
        """
        Génération TTS avec gTTS (gratuit).
        """
        # Mapping langues
        lang_map = {
            "fr": "fr",
            "moore": "fr",  # Pas de support Mooré → fallback français
            "dioula": "fr",
            "fulfulde": "fr",
        }
        
        tts_lang = lang_map.get(language, "fr")
        
        # Générer l'audio
        tts = gTTS(text=text, lang=tts_lang, slow=False)
        
        # Sauvegarder localement ou sur cloud storage
        filename = f"alert_{datetime.now().timestamp()}.mp3"
        filepath = f"/tmp/{filename}"  # Ou S3/Azure Blob
        
        tts.save(filepath)
        
        # Upload vers cloud (exemple avec Azure Blob)
        audio_url = await self._upload_to_cloud(filepath)
        
        return audio_url
    
    async def _azure_tts(self, text: str, language: str) -> str:
        """
        Génération TTS avec Azure Speech (premium, inclus GitHub Education).
        
        Avantages :
        - Voix neuronales ultra-réalistes
        - Support dialectes africains via SSML
        """
        import azure.cognitiveservices.speech as speechsdk
        
        # Configuration
        speech_config = speechsdk.SpeechConfig(
            subscription=self.azure_speech_key,
            region=self.azure_region
        )
        
        # Mapping voix neuronales
        voice_map = {
            "fr": "fr-FR-DeniseNeural",  # Voix féminine française
            "moore": "fr-FR-DeniseNeural",  # Fallback
            "dioula": "fr-FR-DeniseNeural",
        }
        
        speech_config.speech_synthesis_voice_name = voice_map.get(language, "fr-FR-DeniseNeural")
        
        # Générer
        audio_config = speechsdk.audio.AudioOutputConfig(filename=f"/tmp/alert_{datetime.now().timestamp()}.mp3")
        synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)
        
        result = synthesizer.speak_text_async(text).get()
        
        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            audio_url = await self._upload_to_cloud(audio_config.filename)
            return audio_url
        else:
            raise Exception(f"Erreur Azure TTS : {result.reason}")
    
    async def _upload_to_cloud(self, filepath: str) -> str:
        """
        Upload fichier audio vers cloud storage.
        
        Options :
        - Azure Blob Storage (GitHub Education)
        - AWS S3
        - DigitalOcean Spaces
        """
        # Exemple simplifié (à adapter)
        # from azure.storage.blob import BlobServiceClient
        
        # blob_service = BlobServiceClient(
        #     account_url=f"https://{os.getenv('AZURE_STORAGE_ACCOUNT')}.blob.core.windows.net",
        #     credential=os.getenv('AZURE_STORAGE_KEY')
        # )
        
        # container = blob_service.get_container_client("alerts-audio")
        # blob_name = os.path.basename(filepath)
        # with open(filepath, "rb") as data:
        #     container.upload_blob(name=blob_name, data=data)
        
        # return f"https://{os.getenv('AZURE_STORAGE_ACCOUNT')}.blob.core.windows.net/alerts-audio/{blob_name}"
        
        # Pour le moment, retourner un placeholder
        return f"https://agribot-storage.blob.core.windows.net/audio/{os.path.basename(filepath)}"
    
    async def _send_notification(self, user, alert, audio_url: str = None) -> bool:
        """
        Envoie la notification à un utilisateur via WhatsApp.
        """
        try:
            # Message texte
            body_text = f"{alert.title}\n\n{alert.message}"
            
            # Envoyer via Twilio WhatsApp
            message = self.twilio_client.messages.create(
                from_=self.twilio_whatsapp_number,
                to=f"whatsapp:{user.phone}",
                body=body_text
            )
            
            # Si audio disponible, envoyer en deuxième message
            if audio_url:
                self.twilio_client.messages.create(
                    from_=self.twilio_whatsapp_number,
                    to=f"whatsapp:{user.phone}",
                    media_url=[audio_url]
                )
            
            logger.info(f"✅ Notification envoyée à {user.phone} (SID: {message.sid})")
            return True
            
        except Exception as e:
            logger.error(f"❌ Erreur envoi à {user.phone}: {e}")
            return False
    
    async def _mark_as_processed(self, alert, sent_count: int):
        """
        Marque l'alerte comme traitée.
        """
        await self.db.alert.update(
            where={"id": alert.id},
            data={
                "processed": True,
                "sent_count": sent_count,
                "updatedAt": datetime.utcnow()
            }
        )


# ============================================
# BACKGROUND SCHEDULER
# ============================================

from apscheduler.schedulers.asyncio import AsyncIOScheduler

def start_broadcaster_agent(db_client):
    """
    Démarre le BroadcasterAgent en tâche de fond.
    
    Usage:
        @app.on_event("startup")
        async def startup():
            start_broadcaster_agent(prisma_client)
    """
    scheduler = AsyncIOScheduler()
    broadcaster = BroadcasterAgent(db_client)
    
    # Exécuter toutes les 5 minutes
    scheduler.add_job(
        broadcaster.run_broadcast_cycle,
        'interval',
        minutes=5,
        id='broadcaster_cycle',
        replace_existing=True
    )
    
    scheduler.start()
    logger.info("📢 BroadcasterAgent démarré (cycle toutes les 5min)")


if __name__ == "__main__":
    # Test standalone
    import asyncio
    from prisma import Prisma
    
    async def test():
        db = Prisma()
        await db.connect()
        
        broadcaster = BroadcasterAgent(db)
        await broadcaster.run_broadcast_cycle()
        
        await db.disconnect()
    
    asyncio.run(test())
