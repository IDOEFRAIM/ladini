"""`core/geofencing.py` — rectangle englobant Burkina Faso pour le service
de livraison GPS."""
from __future__ import annotations

from ladini.core.geofencing import is_within_burkina_faso


class TestIsWithinBurkinaFaso:
    def test_ouagadougou_is_inside(self):
        # Ouagadougou ~ 12.37, -1.52
        assert is_within_burkina_faso(12.37, -1.52) is True

    def test_bobo_dioulasso_is_inside(self):
        # Bobo-Dioulasso ~ 11.18, -4.30
        assert is_within_burkina_faso(11.18, -4.30) is True

    def test_exact_boundary_values_are_inside(self):
        assert is_within_burkina_faso(9.3, -5.5) is True
        assert is_within_burkina_faso(15.1, 2.4) is True

    def test_just_outside_the_boundary_is_rejected(self):
        assert is_within_burkina_faso(9.29, 0.0) is False
        assert is_within_burkina_faso(15.11, 0.0) is False
        assert is_within_burkina_faso(12.0, -5.51) is False
        assert is_within_burkina_faso(12.0, 2.41) is False

    def test_abidjan_ivory_coast_is_outside(self):
        # Abidjan ~ 5.35, -4.03 (Côte d'Ivoire, sud du Burkina)
        assert is_within_burkina_faso(5.35, -4.03) is False

    def test_niamey_niger_is_outside(self):
        # Niamey ~ 13.51, 2.11 -- proche mais à l'est de la limite
        assert is_within_burkina_faso(13.51, 2.5) is False

    def test_paris_is_wildly_outside(self):
        assert is_within_burkina_faso(48.85, 2.35) is False

    def test_non_numeric_input_never_raises(self):
        assert is_within_burkina_faso("abc", "def") is False
        assert is_within_burkina_faso(None, None) is False
