"""Face recognition: who is in front of the robot?

Embeddings live in data/faces.npz — one averaged embedding per person.
Enroll people with enroll.py.
"""

from pathlib import Path

import numpy as np

from .config import ROOT

DATA = ROOT / "data"
STORE = DATA / "faces.npz"


class FaceMemory:
    def __init__(self, match_threshold: float = 0.45):
        self.match_threshold = match_threshold
        # insightface downloads its model pack on first use (~/.insightface)
        from insightface.app import FaceAnalysis

        self.app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        self.app.prepare(ctx_id=-1, det_size=(640, 640))
        self.known: dict[str, np.ndarray] = {}
        self._load()

    def _load(self):
        if STORE.exists():
            with np.load(STORE) as z:
                self.known = {name: z[name] for name in z.files}

    def _save(self):
        DATA.mkdir(exist_ok=True)
        np.savez(STORE, **self.known)

    def enroll(self, name: str, embeddings: list[np.ndarray]):
        emb = np.mean(np.stack(embeddings), axis=0)
        self.known[name] = emb / np.linalg.norm(emb)
        self._save()

    def detect(self, frame_bgr: np.ndarray):
        """Return list of (name_or_None, similarity, face) for each face in frame."""
        results = []
        for face in self.app.get(frame_bgr):
            emb = face.normed_embedding
            name, best = None, 0.0
            for known_name, known_emb in self.known.items():
                sim = float(np.dot(emb, known_emb))
                if sim > best:
                    name, best = known_name, sim
            if best < self.match_threshold:
                name = None
            results.append((name, best, face))
        return results

    def detect_boxes(self, frame_bgr: np.ndarray):
        """Fast detection-only pass (no identity) — for head tracking.

        Filters low-confidence hits so face-like artwork, screens and
        posters don't hijack the robot's attention.
        """
        bboxes, _ = self.app.det_model.detect(frame_bgr, max_num=0, metric="default")
        return [b for b in bboxes if b[4] > 0.62]

    def biggest_face(self, frame_bgr: np.ndarray):
        """The most prominent face in view, or None."""
        results = self.detect(frame_bgr)
        if not results:
            return None
        return max(results, key=lambda r: _area(r[2]))


def _area(face) -> float:
    x1, y1, x2, y2 = face.bbox
    return float((x2 - x1) * (y2 - y1))


def face_center(face) -> tuple[int, int]:
    x1, y1, x2, y2 = face.bbox
    return int((x1 + x2) / 2), int((y1 + y2) / 2)
