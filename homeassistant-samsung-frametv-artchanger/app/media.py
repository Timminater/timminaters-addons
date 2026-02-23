from __future__ import annotations

import hashlib
from io import BytesIO
from typing import Dict, Optional, Tuple

from PIL import Image, ImageOps


TARGET_WIDTH = 3840
TARGET_HEIGHT = 2160
THUMBNAIL_SIZE = (640, 360)


class MediaService:
    @staticmethod
    def _normalize_orientation(image: Image.Image) -> Image.Image:
        return ImageOps.exif_transpose(image)

    @staticmethod
    def _center_crop(image: Image.Image, target_ratio: float) -> Image.Image:
        width, height = image.size
        ratio = width / height
        if ratio > target_ratio:
            new_width = int(height * target_ratio)
            left = max((width - new_width) // 2, 0)
            return image.crop((left, 0, left + new_width, height))

        new_height = int(width / target_ratio)
        top = max((height - new_height) // 2, 0)
        return image.crop((0, top, width, top + new_height))

    @staticmethod
    def _apply_crop(image: Image.Image, crop: Optional[Dict[str, float]]) -> Image.Image:
        if not crop:
            return MediaService._center_crop(image, TARGET_WIDTH / TARGET_HEIGHT)

        width, height = image.size
        x = float(crop.get("x", 0.0))
        y = float(crop.get("y", 0.0))
        w = float(crop.get("width", width))
        h = float(crop.get("height", height))

        x = max(0.0, min(x, width - 1))
        y = max(0.0, min(y, height - 1))
        w = max(1.0, min(w, width - x))
        h = max(1.0, min(h, height - y))

        return image.crop((int(x), int(y), int(x + w), int(y + h)))

    def prepare_image(self, file_bytes: bytes, crop: Optional[Dict[str, float]] = None) -> Tuple[bytes, str]:
        with Image.open(BytesIO(file_bytes)) as opened:
            image = self._normalize_orientation(opened.convert("RGB"))
            cropped = self._apply_crop(image, crop)
            resized = cropped.resize((TARGET_WIDTH, TARGET_HEIGHT), Image.Resampling.LANCZOS)

            output = BytesIO()
            resized.save(output, format="JPEG", quality=90)
            payload = output.getvalue()

        digest = hashlib.sha256(payload).hexdigest()
        return payload, digest

    def build_thumbnail(self, file_bytes: bytes) -> bytes:
        with Image.open(BytesIO(file_bytes)) as opened:
            image = self._normalize_orientation(opened.convert("RGB"))
            thumb = self._center_crop(image, THUMBNAIL_SIZE[0] / THUMBNAIL_SIZE[1])
            thumb = thumb.resize(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)

            output = BytesIO()
            thumb.save(output, format="JPEG", quality=85)
            return output.getvalue()
