"""Lightweight result helpers shared by runtime adapters."""
import json

IMAGE_PATH_KEY = "image_path"
SOM_IMAGE_PATH_KEY = "SoM_image_path"


def extract_image_path(result) -> str | None:
    """Extract local image path from a tool result (dict or JSON string)."""
    obj = None
    if isinstance(result, dict):
        obj = result
    elif isinstance(result, str):
        try:
            obj = json.loads(result)
        except (ValueError, TypeError):
            pass
    if obj and isinstance(obj, dict):
        path = obj.get(SOM_IMAGE_PATH_KEY) or obj.get(IMAGE_PATH_KEY)
        if path:
            return path
    return None
