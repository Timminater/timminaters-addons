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
