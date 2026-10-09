"""Materialize a built wheel as a Hermes directory plugin, without activation.

Usage: python scripts/install_artifact.py path/to/local_first_review.whl --home /explicit/home
Requires pip in this interpreter. Refuses existing destinations; never upgrades live state.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    if wheel.suffix != '.whl':
        parser.error('provide a built .whl artifact')
    destination = args.home.expanduser().resolve() / 'plugins' / 'local-first-review'
    if destination.exists():
        parser.error(f'{destination} already exists; installation is intentionally not an in-place upgrade')
    destination.mkdir(parents=True)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-compile', '--target', str(destination), str(wheel)], check=True)
    assets = destination / 'share/hermes/plugins/local-first-review'
    for name in ('plugin.yaml', '__init__.py'):
        shutil.copy2(assets / name, destination / name)
    shutil.copytree(assets / 'dashboard', destination / 'dashboard')
    print(f'Installed directory plugin at {destination}. Not enabled or activated.')
    print('Enable only after reviewing policy/configuration, in the intended Hermes home.')


if __name__ == '__main__':
    main()
