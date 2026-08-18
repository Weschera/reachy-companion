"""Real vision: NVIDIA Cosmos-Reason2 on a DGX Spark looks at camera frames."""

import base64

import cv2
from openai import OpenAI


class Eyes:
    def __init__(self, cfg: dict):
        self.client = OpenAI(base_url=cfg["base_url"], api_key="none", timeout=60)
        self.model = cfg["model"]
        self.max_tokens = int(cfg.get("max_tokens", 700))

    def look(self, frame_bgr, question: str) -> str:
        ok, jpg = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return "My camera glitched, sorry."
        b64 = base64.b64encode(jpg.tobytes()).decode()
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[
                {
                    "role": "system",
                    "content": "You are the vision system of a small desk robot. "
                    "Look at the camera image and answer the question in 1-2 "
                    "short spoken sentences. Be concrete about what you see.",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                        },
                        {"type": "text", "text": question},
                    ],
                },
            ],
        )
        return (response.choices[0].message.content or "").strip()
