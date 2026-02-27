{
  "mcpServers": {
    "agri-db": {
      "command": "python",
      "args": [
        "-m",
        "backend.src.agriconnect.servers.agri_db_server"
      ],
      "env": {
        "DATABASE_URL": "postgresql://doadmin:VOTRE_MOT_DE_PASSE@db-postgresql-fra1...ondigitalocean.com:25060/defaultdb?sslmode=require",
        "PYTHONPATH": "."
      }
    },
    "agri-rag": {
      "command": "python",
      "args": [
        "-m",
        "backend.src.agriconnect.servers.agri_rag_server"
      ],
      "env": {
        "PYTHONPATH": ".",
        "OPENAI_API_KEY": "votre_cle_si_besoin_pour_embeddings"
      }
    }
  }
}