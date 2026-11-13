"""Image API adapter; generated files stay inside the project figures directory."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from openai import OpenAI
from PIL import Image

from paper_agent.integrations.model import record_usage
from paper_agent.storage import atomic_bytes


def generate(project: Path, prompt: str, output_path: str, *, size: str, quality: str) -> dict:
    project = project.resolve()
    output = (project / output_path).resolve()
    figures = (project / 'figures').resolve()
    if not figures.is_relative_to(project) or figures not in output.parents or output.suffix.lower() != '.png':
        raise ValueError('output_path must name a PNG under project figures/')
    if not prompt.strip():
        raise ValueError('prompt is required')
    api_key = os.environ.get('PAPER_AGENT_IMAGE_API_KEY') or os.environ.get('PAPER_AGENT_API_KEY')
    if not api_key:
        raise ValueError('Configure PAPER_AGENT_IMAGE_API_KEY or PAPER_AGENT_API_KEY')
    model = os.environ.get('PAPER_AGENT_IMAGE_MODEL', 'gpt-image-2')
    client = OpenAI(api_key=api_key,
                    base_url=os.environ.get('PAPER_AGENT_IMAGE_BASE_URL') or os.environ.get('PAPER_AGENT_BASE_URL'),
                    max_retries=0, timeout=600)
    with client:
        response = client.images.generate(model=model, prompt=prompt, size=size, quality=quality)
    record_usage(project, source='image', model=model, phase='paper_figure',
                 usage=getattr(response, 'usage', None))
    encoded = response.data[0].b64_json
    if not encoded:
        raise RuntimeError('Image API returned no image')
    content = base64.b64decode(encoded, validate=True)
    with Image.open(io.BytesIO(content)) as image:
        if image.format != 'PNG':
            raise ValueError('Image API did not return a PNG')
        image.verify()
    atomic_bytes(output, content)
    return {'status': 'completed', 'output_path': output.relative_to(project).as_posix(), 'model': model}
