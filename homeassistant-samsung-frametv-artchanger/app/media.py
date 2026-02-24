from __future__ import annotations

import hashlib
from io import BytesIO
from typing import Any, Dict, Optional, Tuple

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
    def _rotation_from_crop(crop: Optional[Dict[str, Any]]) -> float:
        if not crop:
            return 0.0
        try:
            return float(crop.get("rotation", 0.0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _quarter_turns_from_crop(crop: Optional[Dict[str, Any]]) -> int:
        if not crop:
            return 0
        try:
            turns = int(crop.get("quarter_turns", 0))
        except (TypeError, ValueError):
            return 0
        return turns % 4

    @staticmethod
    def _flip_horizontal_from_crop(crop: Optional[Dict[str, Any]]) -> bool:
        if not crop:
            return False
        return bool(crop.get("flip_horizontal", False))

    @staticmethod
    def _apply_flip_horizontal(image: Image.Image, flip_horizontal: bool) -> Image.Image:
        if not flip_horizontal:
            return image
        return ImageOps.mirror(image)

    @staticmethod
    def _apply_quarter_turns(image: Image.Image, quarter_turns: int) -> Image.Image:
        turns = quarter_turns % 4
        if turns == 0:
            return image
        result = image
        for _ in range(turns):
            # Pillow's ROTATE_270 equals +90 degrees clockwise.
            result = result.transpose(Image.Transpose.ROTATE_270)
        return result

    @staticmethod
    def _apply_rotation(image: Image.Image, rotation: float) -> Image.Image:
        if abs(rotation) < 0.001:
            return image
        return image.rotate(-rotation, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=(0, 0, 0))

    @staticmethod
    def _apply_crop(image: Image.Image, crop: Optional[Dict[str, Any]]) -> Image.Image:
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

    def prepare_image(self, file_bytes: bytes, crop: Optional[Dict[str, Any]] = None) -> Tuple[bytes, str]:
        with Image.open(BytesIO(file_bytes)) as opened:
            image = self._normalize_orientation(opened.convert("RGB"))
            flip_horizontal = self._flip_horizontal_from_crop(crop)
            quarter_turns = self._quarter_turns_from_crop(crop)
            rotation = self._rotation_from_crop(crop)
            transformed = self._apply_flip_horizontal(image, flip_horizontal)
            transformed = self._apply_quarter_turns(transformed, quarter_turns)
            rotated = self._apply_rotation(transformed, rotation)
            cropped = self._apply_crop(rotated, crop)
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
