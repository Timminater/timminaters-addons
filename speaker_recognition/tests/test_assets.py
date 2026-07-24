import json
import re
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).parents[1]


def test_home_assistant_brand_icons_are_valid_transparent_pngs():
    brand = ROOT / "integration" / "speaker_recognition" / "brand"
    for name, size in (("icon.png", (256, 256)), ("icon@2x.png", (512, 512))):
        path = brand / name
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        with Image.open(path) as image:
            assert image.size == size
            assert image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0
            assert image.getbbox() is not None


def test_app_store_assets_have_expected_dimensions():
    expected = {"icon.png": (128, 128), "logo.png": (250, 100)}
    for name, size in expected.items():
        with Image.open(ROOT / name) as image:
            assert image.size == size
            assert image.mode == "RGBA"


def test_speech_prompts_are_varied_and_easy_to_read():
    prompts = json.loads((ROOT / "web" / "assets" / "speech-prompts.json").read_text())

    assert len(prompts) == 50
    assert len(set(prompts)) == 50
    assert all(18 <= len(prompt.split()) <= 45 for prompt in prompts)


def test_recognition_modal_supports_all_capture_sources():
    document = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'id="test-dialog"' in document
    assert 'id="test-audio-file"' in document
    assert 'id="test-record-button"' in document
    assert 'id="test-voice-satellite"' in document
    assert 'id="test-voice-record-button"' in document
    assert 'id="recognize-sample"' in document
    assert 'captureFromSatellite("test")' in script
    assert 'toggleRecording("test")' in script
    assert "renderTestSample()" in script


def test_version_two_ui_exposes_analysis_calibration_and_safe_audio_management():
    document = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "assets" / "app.js").read_text(encoding="utf-8")

    for page in ("profiles-page", "analysis-page", "calibration-page"):
        assert f'id="{page}"' in document
    for control in (
        "analysis-outcome",
        "analysis-source",
        "analysis-waveform",
        "promote-dialog",
        "profile-delete-dialog",
        "calibration-chart",
        "unknown-speaker-policy",
        "extraction-mode",
        "save-policy",
    ):
        assert f'id="{control}"' in document
    assert 'request("api/analyze"' in script
    assert 'source: "test"' in script
    assert 'api/analysis/${encodeURIComponent(id)}/audio?variant=${variant}' in script
    assert 'audio_action: "archive"' in script
    assert 'audio_action: "delete"' in script
    assert 'request("api/pipeline-policy"' in script
    assert "URL.revokeObjectURL" in script


def test_analysis_ui_exposes_denoised_variant_and_async_action():
    document = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "assets" / "app.js").read_text(encoding="utf-8")

    for control in (
        "analysis-original-audio",
        "analysis-denoised-audio",
        "extract-audio",
    ):
        assert f'id="{control}"' in document
    assert 'id="analysis-isolated-audio"' not in document
    assert "Ruis onderdrukken" in document
    assert "/process`" in script
    assert "pollProcessing" in script
    assert "Extra audiobewerking" in script
    assert "Totale pipeline" in script
    assert "Koude start; timing uitgesloten" in script


def test_primary_navigation_lives_in_the_sticky_topbar():
    document = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "assets" / "styles.css").read_text(encoding="utf-8")
    topbar = document.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]

    assert '<nav class="app-nav" aria-label="Hoofdnavigatie">' in topbar
    assert topbar.index('class="brand"') < topbar.index('class="app-nav"')
    assert topbar.index('class="app-nav"') < topbar.index('class="header-meta"')
    assert ".topbar .app-nav" in styles
    assert "flex-wrap:wrap" in styles


def test_every_cached_ui_element_exists_in_the_document():
    document = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "assets" / "app.js").read_text(encoding="utf-8")
    block = script.split("const elements = Object.fromEntries([", 1)[1].split(
        "].join(\" \")", 1
    )[0]
    element_ids = " ".join(re.findall(r'"([a-z0-9 -]+)"', block)).split()

    assert element_ids
    assert not [element_id for element_id in element_ids if f'id="{element_id}"' not in document]
