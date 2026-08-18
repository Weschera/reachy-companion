"""The brain: local Qwen3.8 on the Spark for fast chat, with real tasks
handed off to the Hermes agent (which has tools: files, vault, web, email)."""

import subprocess

from openai import OpenAI


class Brain:
    def __init__(self, cfg: dict):
        self.client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])
        self.model = cfg["model"]
        self.extra_body = cfg.get("extra_body", {})
        self.max_tokens = int(cfg.get("max_tokens", 200))
        self.system_prompt = cfg["system_prompt"]
        # one conversation history per person
        self.histories: dict[str, list[dict]] = {}

    def reply(self, person: str, style: str, text: str) -> str:
        history = self.histories.setdefault(person, [])
        history.append({"role": "user", "content": text})
        history[:] = history[-20:]  # keep it light
        messages = [
            {
                "role": "system",
                "content": f"{self.system_prompt}\n\nYou are talking to: {person}. {style}",
            },
            *history,
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=self.max_tokens,
            extra_body=self.extra_body,
        )
        answer = (response.choices[0].message.content or "").strip()
        history.append({"role": "assistant", "content": answer})
        return answer

    def ask_hermes(self, person: str, task: str) -> str:
        """Hand a real task to the Hermes agent. Slower, but it has hands."""
        try:
            result = subprocess.run(
                [
                    "hermes",
                    "-z",
                    f"(Spoken request from {person}, relayed by the Reachy desk "
                    f"robot. Answer in 1-3 short spoken sentences, no markdown.) "
                    f"{task}",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            answer = result.stdout.strip().splitlines()
            return answer[-1] if answer else "Hermes didn't answer, sorry."
        except subprocess.TimeoutExpired:
            return "That took too long, I gave up on it."
        except Exception:
            return "I couldn't reach Hermes just now."

    def greeting(self, person: str, style: str) -> str:
        return self.reply(
            person,
            style,
            "[The camera just spotted this person arriving at the desk. "
            "Greet them briefly in your own words.]",
        )
