"""`core/maintenance.py` — mécanisme de pause des producteurs Celery
(2026-09-20, cutover Upstash → Valkey). Aucun mécanisme de maintenance
n'existait avant ce chantier — verrouille le contrat exact :
`os.path.exists(MAINTENANCE_FLAG_PATH)`, jamais une exception qui
bloquerait le trafic normal en cas de panne du check lui-même."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

from ladini.core import maintenance


class TestIsCeleryProducerPaused:
    def test_returns_false_when_flag_file_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag_path = os.path.join(tmp, "does_not_exist")
            with patch.object(maintenance, "MAINTENANCE_FLAG_PATH", flag_path):
                assert maintenance.is_celery_producer_paused() is False

    def test_returns_true_when_flag_file_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag_path = os.path.join(tmp, "maintenance_flag")
            with open(flag_path, "w") as f:
                f.write("")
            with patch.object(maintenance, "MAINTENANCE_FLAG_PATH", flag_path):
                assert maintenance.is_celery_producer_paused() is True

    def test_toggling_the_flag_file_toggles_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag_path = os.path.join(tmp, "maintenance_flag")
            with patch.object(maintenance, "MAINTENANCE_FLAG_PATH", flag_path):
                assert maintenance.is_celery_producer_paused() is False
                open(flag_path, "w").close()
                assert maintenance.is_celery_producer_paused() is True
                os.remove(flag_path)
                assert maintenance.is_celery_producer_paused() is False

    def test_fail_open_on_unreadable_path_never_raises(self):
        # Un chemin dont un composant est en fait un FICHIER (pas un
        # répertoire) fait lever OSError/NotADirectoryError à `os.path.exists`
        # sur certains OS — jamais laisser ce mécanisme, par sa propre panne,
        # bloquer le trafic normal (voir docstring du module).
        with tempfile.TemporaryDirectory() as tmp:
            not_a_dir = os.path.join(tmp, "im_a_file")
            open(not_a_dir, "w").close()
            broken_path = os.path.join(not_a_dir, "flag")
            with patch.object(maintenance, "MAINTENANCE_FLAG_PATH", broken_path):
                assert maintenance.is_celery_producer_paused() is False


class TestModuleConstants:
    def test_default_flag_path_is_configurable_via_env(self):
        assert maintenance.MAINTENANCE_FLAG_PATH  # non vide par défaut

    def test_maintenance_message_is_non_empty_and_user_facing(self):
        assert maintenance.MAINTENANCE_MESSAGE
        assert "maintenance" in maintenance.MAINTENANCE_MESSAGE.lower()
