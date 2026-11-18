"""Render current manuscript pages for the reviewer's native image input."""

from pathlib import Path
import subprocess
import time


def manuscript_previews(source: Path, folder: Path, *, deadline: float):
    import pymupdf
    from PIL import Image, ImageDraw

    folder.mkdir(parents=True, exist_ok=True)
    attachments, mapping = [], []
    with pymupdf.open(source) as document:
        if not len(document):
            raise ValueError('Cannot visually review an empty PDF')
        for start in range(0, len(document), 4):
            sheet = Image.new('RGB', (1840, 2480), '#dddddd')
            draw = ImageDraw.Draw(sheet)
            pages = []
            for slot, number in enumerate(range(start, min(start + 4, len(document)))):
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired('render manuscript previews', 0)
                page = document[number]
                scale = 1800 / max(page.rect.width, page.rect.height)
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                picture = Image.frombytes('RGB', (pixmap.width, pixmap.height), pixmap.samples)
                full_page = folder / f'page-{number + 1:04d}.png'
                picture.save(full_page)
                picture.thumbnail((900, 1190), Image.Resampling.LANCZOS)
                left, top = (slot % 2) * 920, (slot // 2) * 1240
                draw.text((left + 10, top + 8), f'PDF page {number + 1}', fill='black')
                sheet.paste(picture, (left + (920 - picture.width) // 2, top + 35))
                pages.append(f'page {number + 1}: {full_page}')
            attachment = folder / f'sheet-{len(attachments) + 1:04d}.jpg'
            sheet.save(attachment, quality=90)
            attachments.append(attachment)
            mapping.append(f'- Attached image {len(attachments)} (row-major order): ' + '; '.join(pages))
    instructions = (
        '\n\nVisual evidence: the current PDF pages are attached as actual images, not encoded text. '
        'Inspect these attachments first. Each sheet has up to four labeled pages, in row-major order. '
        'For small labels or details, use the image-viewing tool on the full-page PNG paths below. '
        'Do not print base64, data URLs or image bytes in shell output; they are text, not visual input. '
        'Do not regenerate previews. If the attachments or image-viewing tool are unavailable, '
        'explicitly report the visual inspection gap; text/font extraction alone is not visual inspection.\n'
        + '\n'.join(mapping) + '\n')
    return attachments, instructions
