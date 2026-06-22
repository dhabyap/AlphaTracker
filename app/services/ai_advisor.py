"""AI Advisor — uses LLM gateway to give token recommendations."""

import os
import json
import requests

LLM_URL = os.environ.get("LLM_URL", "http://localhost:20128/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "groq/llama-3.3-70b-versatile")


class AIAdvisor:
    def __init__(self):
        self.url = LLM_URL
        self.model = LLM_MODEL

    def analyze(self, context: str) -> dict:
        """Get AI recommendation for a token based on market data."""
        prompt = f"""You are a professional crypto analyst. Analyze this token and give a recommendation:

{context}

Respond in JSON format only:
{{
  "verdict": "strong_buy | buy | hold | avoid | strong_sell",
  "confidence": 0-100,
  "reasoning": "2-3 sentence analysis in English, concise and natural",
  "key_factors": ["factor1", "factor2"],
  "risk_level": "low | medium | high",
  "hold_until": "condition for selling (e.g. drop 20% from current price, or gain 50%)"
}}

JSON only, no markdown."""
        try:
            r = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if r.status_code != 200:
                return {"verdict": "unknown", "confidence": 0, "reasoning": f"API error: {r.status_code}"}

            raw = r.text.strip()
            # Strip streaming artifacts (gateway adds data: [DONE])
            if "data: [DONE]" in raw:
                raw = raw.split("data: [DONE]")[0].strip()

            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"].strip()

            # Clean markdown code blocks
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:]) if len(lines) > 1 else lines[0]
            if "```" in content:
                content = content.split("```")[0]

            # Find and extract JSON object
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                content = content[start : end + 1]

            return json.loads(content)

        except json.JSONDecodeError as e:
            return {"verdict": "hold", "confidence": 50, "reasoning": "Data available, AI parsing error.", "risk_level": "medium", "hold_until": "Price below support"}
        except Exception as e:
            return {"verdict": "unknown", "confidence": 0, "reasoning": f"Error: {str(e)}"}
