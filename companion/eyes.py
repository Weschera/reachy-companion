"""Real vision: answer questions about camera frames.

Two modes, picked by config:
  eyes.local_model  → a VLM running right here via MLX (Mac)
  eyes.base_url     → any OpenAI-compatible VLM server (e.g. Cosmos on a Spark)
"""

import base64

import cv2


class Eyes:
    def __init__(self, cfg: dict):
        self.max_tokens = int(cfg.get("max_tokens", 700))
        self.local_model = cfg.get("local_model")
        self._local = None  # lazy-loaded (model, processor, config)
        if not self.local_model:
            from openai import OpenAI

            self.client = OpenAI(base_url=cfg["base_url"], api_key="none", timeout=60)
            self.model = cfg["model"]

    SYSTEM = (
        "You are the vision system of a small desk robot. Look at the camera "
        "image and answer the question in 1-2 short spoken sentences. Be "
        "concrete about what you see."
    )

    def _ensure_local(self):
        if self._local is None:
            from mlx_vlm import load
            from mlx_vlm.utils import load_config

            model, processor = load(self.local_model)
            config = load_config(self.local_model)
            self._local = (model, processor, config)
        return self._local

    def look(self, frame_bgr, question: str) -> str:
        if self.local_model:
            return self._look_local(frame_bgr, question)
        return self._look_remote(frame_bgr, question)

    def _look_local(self, frame_bgr, question: str) -> str:
        import tempfile

        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        model, processor, config = self._ensure_local()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            cv2.imwrite(f.name, frame_bgr)
            prompt = apply_chat_template(
                processor, config, f"{self.SYSTEM}\n\n{question}", num_images=1
            )
            result = generate(
                model, processor, prompt, image=f.name, max_tokens=self.max_tokens
            )
        text = result.text if hasattr(result, "text") else str(result)
        return text.strip()

    def _look_remote(self, frame_bgr, question: str) -> str:
        ok, jpg = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return "My camera glitched, sorry."
        b64 = base64.b64encode(jpg.tobytes()).decode()
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": self.SYSTEM},
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
