"""Teach Reachy a face:  uv run python enroll.py "Name"

Stand in front of the robot; it grabs ~10 good frames, averages the face
embedding, and saves it. Then add/adjust the person's voice in profiles.yaml.
"""

import sys
import time

from reachy_mini import ReachyMini

from companion.config import load_config
from companion.faces import FaceMemory


def main():
    if len(sys.argv) != 2:
        print('usage: uv run python enroll.py "Name"')
        sys.exit(1)
    name = sys.argv[1]
    cfg = load_config()
    faces = FaceMemory(cfg["faces"]["match_threshold"])

    print(f"connecting to reachy at {cfg['robot']['host']}...")
    with ReachyMini(
        host=cfg["robot"]["host"],
        port=cfg["robot"]["port"],
        connection_mode="network",
        media_backend="webrtc",
    ) as mini:
        print(f"look at the robot, {name} — capturing 10 frames...")
        embeddings = []
        while len(embeddings) < 10:
            frame = mini.media.get_frame()
            if frame is None:
                time.sleep(0.2)
                continue
            found = faces.biggest_face(frame)
            if found is None:
                print("  no face in view...")
                time.sleep(0.5)
                continue
            _, _, face = found
            embeddings.append(face.normed_embedding)
            print(f"  captured {len(embeddings)}/10")
            time.sleep(0.3)
        faces.enroll(name, embeddings)
    print(f"done — {name} enrolled. Now give them a voice in profiles.yaml.")


if __name__ == "__main__":
    main()
