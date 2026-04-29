"""Cast contract loader. Reads the non-secret config/podcast-cast.yaml and
produces a validated CastConfig plus a stable byte-fingerprint that the
manifest records under cast_config_hash.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from .config import CAST_CONFIG_PATH
from .schema import CastConfig


def load_cast(path: Path = CAST_CONFIG_PATH) -> CastConfig:
    cfg = CastConfig.model_validate(yaml.safe_load(path.read_text()))
    if cfg.anchor not in cfg.cast:
        raise ValueError(f"anchor {cfg.anchor!r} not in cast slugs {list(cfg.cast.keys())}")
    return cfg


def cast_config_hash(path: Path = CAST_CONFIG_PATH) -> str:
    """Stable 12-char fingerprint of the cast config bytes.

    Hashing the file bytes (not the parsed Pydantic dump) keeps the
    fingerprint comparable from outside the engine.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
