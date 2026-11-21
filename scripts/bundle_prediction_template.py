"""Refresh the packaged prediction template from a reviewed source checkout."""

import argparse
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED


def bundle(source: Path, output: Path):
    files = [source / name for name in ('run.py', 'configuration.py', 'dataloader.py', 'requirements.txt')]
    for folder in ('adapters', 'data_provider', 'models', 'exp', 'utils', 'layers', 'tests'):
        files.extend((source / folder).rglob('*.py'))
    files.extend((source / 'configs').glob('*.json'))
    files.extend((source / 'data_provider').glob('*.json'))
    files.append(source / 'data_provider' / 'LICENSE.BatteryLife')
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            if not path.is_file() or path.is_symlink():
                raise ValueError(f'Missing file or symlink in template: {path}')
            relative = path.relative_to(source)
            if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
                continue
            info = ZipInfo(relative.as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]
                        / 'batteryscientist/templates/template.zip')
    args = parser.parse_args()
    bundle(args.source.resolve(), args.output)
