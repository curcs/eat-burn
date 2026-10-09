"""Под launchd PATH урезан до /usr/bin:/bin — бот должен сам находить tesseract и ffmpeg из Homebrew."""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.mark.skipif(not Path("/opt/homebrew/bin/tesseract").exists(), reason="нужен Homebrew tesseract")
def test_tools_found_with_launchd_path(monkeypatch):
    import bot
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert shutil.which("tesseract") is None
    bot.add_homebrew_to_path()
    assert shutil.which("tesseract") and shutil.which("ffmpeg")
