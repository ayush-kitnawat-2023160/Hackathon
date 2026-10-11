"""Small provider adapter: no model may create allocations or KPIs."""

import json
import os

import httpx


class LanguageModel:
    def __init__(self):
        self.provider = os.getenv("LLM_PROVIDER", "ollama").lower()
        self.model = os.getenv("LLM_MODEL", "qwen2.5:7b-instruct")
        self.mock = os.getenv("MOCK_LLM", "true").lower() in ("true", "1", "yes")
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "15"))

    def _key(self):
        names = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                 "gemini": "GEMINI_API_KEY"}
        return os.getenv(names.get(self.provider, ""), "")

    def ready(self):
        if self.mock:
            return {"status": "mock", "provider": "mock"}
        if self.provider not in ("openai", "anthropic", "gemini", "ollama"):
            return {"status": "unconfigured", "provider": self.provider}
        return {"status": "configured" if self.provider == "ollama" or self._key() else "missing_key",
                "provider": self.provider, "model": self.model}

    def complete(self, system, user, *, json_mode=False):
        if self.mock:
            raise RuntimeError("Mock mode uses deterministic routing and explanations")
        provider = self.provider
        key = self._key()
        if provider != "ollama" and not key:
            raise RuntimeError(f"Missing API key for {provider}")
        with httpx.Client(timeout=self.timeout) as client:
            if provider == "ollama":
                data = {"model": self.model, "stream": False,
                        "messages": [{"role": "system", "content": system},
                                     {"role": "user", "content": user}],
                        "options": {"temperature": 0}}
                if json_mode:
                    data["format"] = "json"
                resp = client.post(os.getenv("OLLAMA_URL", "http://host.docker.internal:11434") + "/api/chat",
                    json=data)
                resp.raise_for_status()
                return resp.json()["message"]["content"]
            if provider == "openai":
                body = {"model": self.model, "messages": [
                    {"role": "system", "content": system}, {"role": "user", "content": user}]}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                resp = client.post("https://api.openai.com/v1/chat/completions", json=body,
                                   headers={"Authorization": f"Bearer {key}"})
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            if provider == "anthropic":
                resp = client.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                    json={"model": self.model, "max_tokens": 650, "temperature": 0,
                          "system": system, "messages": [{"role": "user", "content": user}]})
                resp.raise_for_status()
                return "".join(p["text"] for p in resp.json()["content"] if p["type"] == "text")
            if provider == "gemini":
                resp = client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                    headers={"x-goog-api-key": key},
                    json={"systemInstruction": {"parts": [{"text": system}]},
                          "contents": [{"role": "user", "parts": [{"text": user}]}],
                          "generationConfig": {"temperature": 0,
                                               **({"responseMimeType": "application/json"} if json_mode else {})}})
                resp.raise_for_status()
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        raise ValueError(f"Unsupported LLM provider: {provider}")

    def json(self, system, user):
        text = self.complete(system, user, json_mode=True).strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError("LLM must return a JSON object")
        return result
