"""Use the skill's single source of truth when running from a checkout.

Wheels install these modules directly here using setuptools package-dir.
"""
from pathlib import Path

_source = Path(__file__).resolve().parents[2] / "skills/comment-review-audit/scripts"
if _source.is_dir():
    __path__.append(str(_source))
