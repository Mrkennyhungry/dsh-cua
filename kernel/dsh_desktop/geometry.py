"""Physical-pixel geometry retained with each emitted image."""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class FrameGeometry:
    source_rect: tuple[int, int, int, int]
    image_size: tuple[int, int]
    client_rect: tuple[int, int, int, int] | None = None
    window_rect: tuple[int, int, int, int] | None = None
    crop: tuple[int, int, int, int] | None = None
    dpi: tuple[int, int] = (96, 96)
    virtual_origin: tuple[int, int] = (0, 0)
    method: str = 'screen'
    coordinate_space: str = 'screen_physical'

    def __post_init__(self):
        values = (*self.source_rect, *self.image_size, *self.dpi, *(self.crop or ()))
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values):
            raise ValueError('Frame geometry must contain finite numbers')
        if min(self.source_rect[2:]) <= 0 or min(self.image_size) <= 0:
            raise ValueError('Frame dimensions must be positive')
        if min(self.dpi) <= 0:
            raise ValueError('Frame DPI must be positive')
        if self.crop is not None:
            x, y, width, height = self.crop
            if min(x, y) < 0 or min(width, height) <= 0 or x + width > self.source_rect[2] or y + height > self.source_rect[3]:
                raise ValueError('Crop must fit within the source frame')

    def to_source(self, x: float, y: float) -> tuple[float, float]:
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in (x, y)):
            raise ValueError('Image coordinates must be finite numbers')
        if not 0 <= x < self.image_size[0] or not 0 <= y < self.image_size[1]:
            raise ValueError('Point lies outside the emitted image')
        left, top, width, height = self.crop or (0, 0, *self.source_rect[2:])
        return left + x * width / self.image_size[0], top + y * height / self.image_size[1]

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        px, py = self.to_source(x, y)
        # Rounding the final subpixel must never escape the last source pixel.
        left, top, width, height = self.crop or (0, 0, *self.source_rect[2:])
        return (min(round(self.source_rect[0] + px), self.source_rect[0] + left + width - 1),
                min(round(self.source_rect[1] + py), self.source_rect[1] + top + height - 1))

    def from_screen(self, x: float, y: float) -> tuple[float, float]:
        left, top, width, height = self.crop or (0, 0, *self.source_rect[2:])
        return ((x - self.source_rect[0] - left) * self.image_size[0] / width,
                (y - self.source_rect[1] - top) * self.image_size[1] / height)

    def metadata(self):
        extent = self.crop[2:] if self.crop else self.source_rect[2:]
        return {**asdict(self), 'model_rect': (0, 0, *self.image_size),
                'scale': (self.image_size[0] / extent[0], self.image_size[1] / extent[1]),
                'screen_rect': self.source_rect if self.coordinate_space == 'screen_physical' else None}
