import os
import json
from twilio.rest import Client

def send_whatsapp_template(to_number: str, content_sid: str, variables: dict):
    # Remplace CES VALEURS par tes vraies clés, sans utiliser os.getenv pour le moment
    # Assure-toi qu'il n'y a PAS d'espaces avant ou après
    sid = "ACcd7d65c7687f8672af5c6ed59ebc4f3a"
    token = "4b95d9e3aa5830f96e149590a2ed86a7"
    
    # Debug : vérification simple
    print(f"DEBUG: SID est bien ACcd7d65...")
    print(f"DEBUG: Token commence par: {token[:4]}...")

    client = Client(sid, token)
    
    try:
        message = client.messages.create(
            from_="whatsapp:+14155238886",
            to=f"whatsapp:{to_number}",
            content_sid=content_sid,
            content_variables=json.dumps(variables)
        )
        print(f"Succès ! SID du message : {message.sid}")
    except Exception as e:
        print(f"ERREUR DÉTAILLÉE : {e}")

if __name__ == "__main__":
    send_whatsapp_template(
        to_number="+22601479800", 
        content_sid="HXb5b62575e6e4ff6129ad7c8efe1f983e", 
        variables={"1": "12/1", "2": "3pm"}
    )