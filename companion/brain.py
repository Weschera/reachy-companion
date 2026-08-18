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
        self.hermes_provider = cfg.get("hermes_provider")
        self.hermes_model = cfg.get("hermes_model")
        # one conversation history per person
        self.histories: dict[str, list[dict]] = {}

    def reply(self, person: str, style: str, text: str) -> str:
        history = self.histories.setdefault(person, [])
        history[:] = [m for m in history if m["content"]]  # drop any blanks
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
        if not answer:
            # model went blank — don't leave a hole in the conversation
            answer = "Hm, I lost my train of thought. What were you saying?"
        history.append({"role": "assistant", "content": answer})
        return answer

    def ask_hermes(self, person: str, task: str) -> str:
        """Hand a real task to the Hermes agent. Slower, but it has hands."""
        try:
            cmd = [
                "hermes",
                "-z",
                f"(Spoken request from {person}, relayed by the Reachy desk "
                f"robot. Answer in 1-3 short spoken sentences, no markdown.) "
                f"{task}",
            ]
            if self.hermes_provider and self.hermes_model:
                cmd += ["--provider", self.hermes_provider, "-m", self.hermes_model]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            answer = " ".join(
                line.strip() for line in result.stdout.strip().splitlines() if line.strip()
            )
            return answer or "Hermes didn't answer, sorry."
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
