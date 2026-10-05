from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from archive import gui


class GuiThemeTests(unittest.TestCase):
    def test_system_theme_uses_gnome_color_scheme(self) -> None:
        with patch.object(gui.sys, "platform", "linux"), patch(
            "archive.gui.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="'prefer-dark'\n"),
        ):
            self.assertEqual(gui._detect_system_theme(), "dark")

    def test_system_theme_falls_back_to_gtk_theme(self) -> None:
        with (
            patch.object(gui.sys, "platform", "linux"),
            patch("archive.gui.subprocess.run", side_effect=FileNotFoundError),
            patch.dict("archive.gui.os.environ", {"GTK_THEME": "Adwaita:dark"}),
        ):
            self.assertEqual(gui._detect_system_theme(), "dark")

    def test_system_theme_is_unknown_when_no_preference_is_available(self) -> None:
        with (
            patch.object(gui.sys, "platform", "linux"),
            patch(
                "archive.gui.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout=""),
            ),
            patch.dict("archive.gui.os.environ", {}, clear=True),
        ):
            self.assertIsNone(gui._detect_system_theme())


if __name__ == "__main__":
    unittest.main()
