import ee
from gee_auth import initialize_ee

class FAOProvider:
    def __init__(self):
        initialize_ee()
        # Exemple avec le pH du sol (OpenLandMap)
        self.dataset = ee.Image("OpenLandMap/SOL/SOL_PH-H2O_USDA-4A1C_M/v02")

    def get_soil_data(self, lat, lon):
        point = ee.Geometry.Point([lon, lat])
        # On prend la profondeur 0-5cm (bande 'phh2o_mean_0-5cm')
        value = self.dataset.select('phh2o_mean_0-5cm').reduceRegion(
            reducer=ee.Reducer.first(),
            geometry=point,
            scale=250
        ).getInfo()
        return {
            "source": "OpenLandMap",
            "ph_0_5cm": value.get('phh2o_mean_0-5cm')
        }

if __name__ == "__main__":
    lat, lon = 11.17, -4.29
    provider = FAOProvider()
    result = provider.get_soil_data(lat, lon)
    if result:
        print("Résultat OpenLandMap Soil Data :")
        for k, v in result.items():
            print(f"  {k}: {v}")
    else:
        print("Aucune donnée sol disponible pour ce point.")