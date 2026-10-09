"""Small helpers shared by every stage of the pipeline."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

# Default strength of an edge for each confidence label.
CONFIDENCE = {"EXTRACTED": 1.0, "INFERRED": 0.7, "AMBIGUOUS": 0.3}


def node(id: str, label: str, kind: str, source_file: str = "", **extra) -> dict:
    data = {"id": id, "label": label, "kind": kind, "source_file": source_file}
    data.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    return data


def edge(source: str, target: str, relation: str, confidence: str = "EXTRACTED",
         score: float | None = None, source_file: str = "") -> dict:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": confidence,
        "score": CONFIDENCE[confidence] if score is None else score,
        "source_file": source_file,
    }


def slug(text: str) -> str:
    """Normalise a label so that 'Self-Attention' and 'self attention' become the same node."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    result = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    # Labels in non-Latin scripts would otherwise collapse to nothing.
    return result or re.sub(r"\s+", "-", text.strip().lower())


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def read_json(path: Path, default=None):
    try:
        return json.loads(read_text(path))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
