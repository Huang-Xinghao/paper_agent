"""Keep immutable visual evidence outside repeatedly saved role transcripts."""

import base64
import hashlib
from pathlib import Path
import re

from paper_agent.storage import atomic_bytes


class ImageStore:
    def __init__(self, project):
        self.root = Path(project).resolve() / '.paper-agent' / 'images'

    def externalize(self, messages):
        """Replace inline pixels in place; message indexes and tool IDs stay intact."""
        for message in messages:
            content = message.get('content')
            if not isinstance(content, list):
                continue
            for index, block in enumerate(content):
                if block.get('type') != 'image_url':
                    continue
                value = block.get('image_url', {})
                url = value.get('url', '')
                match = re.fullmatch(r'data:(image/(?:png|jpeg));base64,(.+)', url, re.DOTALL)
                if not match:
                    continue
                raw = base64.b64decode(match[2], validate=True)
                digest = hashlib.sha256(raw).hexdigest()
                target = self.root / digest
                if not target.exists():
                    atomic_bytes(target, raw)
                content[index] = {'type': 'stored_image', 'sha256': digest,
                                  'mime_type': match[1], 'detail': value.get('detail', 'auto')}

    def hydrate(self, block):
        digest, mime = block.get('sha256', ''), block.get('mime_type')
        if not re.fullmatch(r'[a-f0-9]{64}', digest) or mime not in {'image/png', 'image/jpeg'}:
            raise ValueError('Invalid stored image reference')
        target = self.root / digest
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('Stored image resolves outside image store')
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Stored image content does not match its hash')
        return {'type': 'image_url', 'image_url': {
            'url': f'data:{mime};base64,' + base64.b64encode(raw).decode('ascii'),
            'detail': block['detail']}}
