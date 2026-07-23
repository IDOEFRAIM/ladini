import sys
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

from futur.tools import sentinelle as sent_mod


def test_resolve_coords_defaults():
    tool = sent_mod.SentinelleTool()
    lat, lon = tool._resolve_coords("")
    assert isinstance(lat, float) and isinstance(lon, float)


def test_resolve_coords_known_city():
    tool = sent_mod.SentinelleTool()
    lat, lon = tool._resolve_coords("ouaga centre")
    # ouaga mapping exists
    assert round(lat, 3) == 12.371 and lon != 0


def test_parse_temperatures_fallback():
    tool = sent_mod.SentinelleTool()
    res = tool._parse_temperatures({})
    assert res["t_min"] == 22.0 and res["t_max"] == 32.0


def test_parse_precip_humidity_wind_values():
    tool = sent_mod.SentinelleTool()
    w = {"forecast_precip_mm": 12, "humidity": 55, "wind_speed": 10}
    res = tool._parse_precip_humidity_wind(w)
    assert res["precip"] == 12.0
    assert res["humidity"] == 55.0
    assert res["wind"] == 10.0


def test_parse_dry_and_ndvi_and_doy_lat():
    tool = sent_mod.SentinelleTool()
    weather = {"dry_days_ahead": "3", "doy": "200", "lat": "13.5"}
    sat = {"ndvi_anomaly": "-0.25"}
    misc1 = tool._parse_dry_and_ndvi(weather, sat)
    misc2 = tool._parse_doy_and_lat(weather)
    assert misc1["dry_days"] == 3
    assert misc1["ndvi"] == -0.25
    assert misc2["doy"] == 200
    assert abs(misc2["lat"] - 13.5) < 0.001


def test_compute_soil_moisture_idx():
    tool = sent_mod.SentinelleTool()
    val = tool._compute_soil_moisture_idx(precip=10, humidity=50, et0=2)
    assert 0.0 <= val <= 1.0


def test_score_from_precip_and_level():
    tool = sent_mod.SentinelleTool()
    assert tool._score_from_precip(35) == 2.5
    assert tool._score_from_precip(15) == 1.0
    level, msg = tool._flood_level_from_score(4.5)
    assert level == "CRITIQUE"
