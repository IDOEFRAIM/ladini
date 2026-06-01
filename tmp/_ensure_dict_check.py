from agriconnect.graphs.agents.market_coach.nodes import _ensure_dict

outer = {
    "status": "ok",
    "data": {
        "raw_result": "{'status': 'error', 'data': None, 'message': 'Erreur base de données : Database operation failed: create_product'}"
    },
    "message": "Raw string converted to dict",
}

print(_ensure_dict(outer))
