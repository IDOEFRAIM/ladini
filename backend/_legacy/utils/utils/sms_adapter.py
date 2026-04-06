import re

class SMSAdapter:
    """
    Middleware pour adapter les réponses riches de l'IA aux contraintes SMS (160 caractères ou concision extrême).
    Pilier 4 : Scalabilité & Omnicanalité.
    """
    
    @staticmethod
    def compress_for_sms(text: str, max_length: int = 320) -> str:
        """
        Réduit drastiquement la réponse pour tenir dans 1 ou 2 SMS (160-320 chars).
        Supprime le markdown, les formules de politesse et les détails superflus.
        """
        if not text:
            return ""

        # 1. Suppression du Markdown riche
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)  # Bold
        text = re.sub(r'\*(.*?)\*', r'\1', text)      # Italic
        text = re.sub(r'#+\s', '', text)              # Headers
        text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text) # Links

        # 2. Suppression des sauts de ligne multiples
        text = re.sub(r'\n+', ' ', text).strip()

        # 3. Suppression des "Remplissage" (Stopwords communicationnels)
        stopwords = [
            "Bonjour,", "Salut,", "Cher agriculteur,", "Cordialement,", 
            "En tant que votre assistant,", "Je vous conseille de", 
            "D'après mes analyses,", "Voici ce que je préconise :"
        ]
        for word in stopwords:
            text = text.replace(word, "")

        # 4. Remplacement par des pictogrammes (Gain de place)
        replacements = {
            "attention": "⚠️",
            "alerte": "🚨",
            "conseil": "💡",
            "importance": "❗",
            "pluie": "🌧️",
            "soleil": "☀️",
            "argent": "💰",
            "franc cfa": "F",
            "FCFA": "F"
        }
        for k, v in replacements.items():
            text = re.sub(r'\b' + re.escape(k) + r'\b', v, text, flags=re.IGNORECASE)

        # 5. Nettoyage final
        text = ' '.join(text.split()) # Retire les doubles espaces
        
        # 6. Truncate intelligent (si toujours trop long)
        if len(text) > max_length:
            return text[:max_length-3] + "..."
            
        return text

    @staticmethod
    def format_incoming_sms(sms_content: str, sender_id: str) -> dict:
        """
        Transforme un SMS brut en objet de requête pour l'Orchestrateur.
        Gère les codes courts (ex: "PLUIE OUAGA").
        """
        sms_content = sms_content.strip().upper()
        
        # Logique USSD/SMS simplifiée
        if sms_content.startswith("M "): # Météo
            query = f"Météo pour {sms_content[2:]}"
        elif sms_content.startswith("P "): # Prix
            query = f"Prix du marché pour {sms_content[2:]}"
        elif sms_content.startswith("A "): # Alerte
            query = f"Je signale une alerte : {sms_content[2:]}"
        else:
            query = sms_content
            
        return {
            "user_id": sender_id,
            "query": query,
            "channel": "SMS"
        }
