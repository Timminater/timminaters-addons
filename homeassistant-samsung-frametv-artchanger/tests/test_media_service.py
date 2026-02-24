import io
import unittest

from PIL import Image

from app.media import MediaService


class MediaServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = MediaService()

    def _sample_image(self, width=1200, height=900, color=(220, 50, 80)):
        image = Image.new("RGB", (width, height), color)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_prepare_image_resizes_to_target(self):
        payload, digest = self.service.prepare_image(self._sample_image())
        self.assertEqual(len(digest), 64)

        image = Image.open(io.BytesIO(payload))
        self.assertEqual(image.size, (3840, 2160))

    def test_prepare_image_uses_crop_region(self):
        source = self._sample_image(2000, 1000, color=(40, 220, 120))
        payload, _ = self.service.prepare_image(
            source,
            crop={"x": 200, "y": 100, "width": 1200, "height": 675},
        )
        image = Image.open(io.BytesIO(payload))
        self.assertEqual(image.size, (3840, 2160))

    def test_build_thumbnail_dimensions(self):
        thumb = self.service.build_thumbnail(self._sample_image())
        image = Image.open(io.BytesIO(thumb))
        self.assertEqual(image.size, (640, 360))

    def test_prepare_image_supports_rotation(self):
        source = self._sample_image(1400, 900, color=(140, 200, 20))
        payload, _ = self.service.prepare_image(
            source,
            crop={"rotation": 18.0},
        )
        image = Image.open(io.BytesIO(payload))
        self.assertEqual(image.size, (3840, 2160))


if __name__ == "__main__":
    unittest.main()
