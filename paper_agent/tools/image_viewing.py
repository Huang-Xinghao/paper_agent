"""Read local raster images for multimodal model inspection."""
import base64
import hashlib
import io
import json

from .core import ToolContext, ToolResult, tool


@tool(read_only=True)
def view_image(ctx: ToolContext, path: str, detail: str = "high",
               max_side: int = 2048) -> ToolResult:
    """View a local PNG/JPEG as visual model input, not text or OCR.

    Render PDFs to PNG first. Images are resized to max_side (256-4096).
    Use a larger max_side when small plot labels need closer inspection.
    """
    from PIL import Image, ImageOps

    if detail not in {"auto", "low", "high"}:
        raise ValueError("detail must be auto, low or high")
    if type(max_side) is not int or not 256 <= max_side <= 4096:
        raise ValueError("max_side must be an integer from 256 to 4096")
    target = ctx.resolve_read(path)
    if target.stat().st_size > 25 * 1024 * 1024:
        raise ValueError("Image exceeds 25 MiB; export a smaller preview")
    raw = target.read_bytes()
    with Image.open(io.BytesIO(raw)) as source:
        if source.format not in {"PNG", "JPEG"}:
            raise ValueError("Only PNG/JPEG images are supported")
        if source.width * source.height > 40_000_000:
            raise ValueError("Image exceeds 40 million pixels")
        original_size = source.size
        picture = ImageOps.exif_transpose(source).convert("RGB")
        picture.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        encoded = io.BytesIO()
        picture.save(encoded, format="PNG")
        size = picture.size
    metadata = {"path": ctx.display_path(target),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "original_size": list(original_size), "display_size": list(size)}
    url = "data:image/png;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")
    return ToolResult(json.dumps(metadata), metadata=metadata,
                      images=[{"type": "image_url", "image_url": {"url": url, "detail": detail}}])
