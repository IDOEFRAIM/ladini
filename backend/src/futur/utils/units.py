from typing import Dict, Any


class AgriUnitConverter:
    """Convertit des masses d'intrants (kg) vers des contenants usuels locaux."""

    # Équivalences moyennes constatées sur le terrain (en kg)
    CONVERSIONS: Dict[str, float] = {
        "sac_50kg": 50.0,
        "seau_20l": 15.0,            # Seau de 20 L rempli d'engrais
        "boite_tomate_grosse": 0.4,  # Boîte "Gros de l'Idéal"
        "tasse_cafe": 0.15,          # Tasse à café / gobelet
        "boite_tomate_petite": 0.07,
        "boite_allumettes": 0.015,
    }

    @classmethod
    def kg_to_local_units(cls, kg_value: float) -> str:
        """Traduit un poids (kg) en combinaison de contenants locaux."""
        if kg_value <= 0:
            return "0 g"

        if kg_value >= 40.0:
            sacs = int(kg_value // cls.CONVERSIONS["sac_50kg"])
            reste_sac = kg_value % cls.CONVERSIONS["sac_50kg"]
            parts = []
            if sacs > 0:
                parts.append(f"{sacs} sac(s) de 50kg")
            if reste_sac > 0:
                parts.append(cls._convert_medium_doses(reste_sac))
            return " + ".join(parts)

        return cls._convert_medium_doses(kg_value)

    @classmethod
    def _convert_medium_doses(cls, kg_value: float) -> str:
        if kg_value >= 5.0:
            seaux = int(kg_value // cls.CONVERSIONS["seau_20l"])
            reste_seau = kg_value % cls.CONVERSIONS["seau_20l"]
            parts = []
            if seaux > 0:
                parts.append(f"{seaux} seau(x) de 20L")
            if reste_seau > 0:
                parts.append(cls._convert_small_doses(reste_seau))
            return " + ".join(parts)

        return cls._convert_small_doses(kg_value)

    @classmethod
    def _convert_small_doses(cls, kg_value: float) -> str:
        if kg_value >= 0.1:
            grosses_boites = int(kg_value // cls.CONVERSIONS["boite_tomate_grosse"])
            reste_boite = kg_value % cls.CONVERSIONS["boite_tomate_grosse"]
            tasses = round(reste_boite / cls.CONVERSIONS["tasse_cafe"])
            parts = []
            if grosses_boites > 0:
                parts.append(f"{grosses_boites} grosse(s) boîte(s) de tomate")
            if tasses > 0:
                parts.append(f"{tasses} tasse(s) à café")
            return " + ".join(parts) if parts else f"{int(kg_value * 1000)} g"

        allumettes = round(kg_value / cls.CONVERSIONS["boite_allumettes"])
        if allumettes > 0:
            return f"{allumettes} boîte(s) d'allumettes"
        return f"{int(kg_value * 1000)} g"
