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

    def _call_llm(self, prompt: str) -> dict:
        """Call the LLM gateway and parse JSON response."""
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
                raise Exception(f"AI API error: {r.status_code} — {r.text[:200]}")

            raw = r.text.strip()
            if "data: [DONE]" in raw:
                raw = raw.split("data: [DONE]")[0].strip()

            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"].strip()

            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:]) if len(lines) > 1 else lines[0]
            if "```" in content:
                content = content.split("```")[0]

            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                content = content[start:end+1]

            return json.loads(content)
        except json.JSONDecodeError:
            return None
        except Exception as e:
            raise

    def analyze(self, context: str) -> dict:
        """Get AI recommendation for a token based on market data."""
        prompt = f"""Anda adalah analis fundamental kripto profesional. Analisis token ini berdasarkan data fundamental di bawah:

{context}

Evaluasi dengan pendekatan fundamentalis — fokus ke:
1. **Kedalaman Likuiditas** — apakah cukup untuk entry/exit besar?
2. **Dilution Risk** — MC vs FDV, berapa banyak supply belum beredar?
3. **Aktivitas** — volume 24h, buy/sell ratio, volume surge
4. **Maturitas** — umur proyek, jumlah holders (jika ada), pair count
5. **Stabilitas** — price change wajar atau pump & dump?

Balas HANYA dalam format JSON (tanpa markdown):
{{
  "verdict": "strong_buy | buy | hold | avoid | strong_sell",
  "confidence": 0-100,
  "reasoning": "Analisis fundamental 3-4 kalimat dalam Bahasa Indonesia. Jelaskan ALASAN fundamental — jangan cuma bilang bagus/jelek. Contoh: 'Likuiditas $473K cukup untuk entry moderate, MC/FDV 1.0 artinya tidak ada dilution risk. Volume surge +720% menandakan whale interest tinggi. Risiko utama adalah thin liquidity relatif terhadap market cap.'",
  "key_factors": ["faktor fundamental 1", "faktor fundamental 2", "faktor fundamental 3"],
  "risk_level": "low | medium | high",
  "hold_until": "Strategi exit: harga target naik/turun berapa persen, atau kondisi tertentu (contoh: jual kalau vol turun 50% atau jika harga -20% dari entry)"
}}

Hanya JSON, tanpa teks lain."""
        result = self._call_llm(prompt)
        if result:
            return result
        return {"verdict": "hold", "confidence": 50, "reasoning": "Data tersedia, tapi AI gagal memproses. Coba refresh.", "risk_level": "medium", "hold_until": "Harga turun di bawah support"}

    def analyze_whale(self, wallet_label: str, wallet_address: str, trades_summary: str, stats: dict) -> dict:
        """Get AI analysis of whale trading behavior and recommendations."""
        prompt = f"""Anda adalah analis on-chain profesional. Analisis aktivitas whale/kantong besar ini:

Whale: {wallet_label} ({wallet_address[:10]}...)
Win Rate: {stats.get('win_rate', 0)*100:.0f}%
Total Transaksi: {stats.get('total_trades', 0)}
Profit Terakhir: ${stats.get('total_pnl', 0):.0f}

Transaksi terbaru:
{trades_summary}

Balas HANYA dalam format JSON:
{{
  "whale_type": "smart_money | random | dump_seller | accumulative | unknown",
  "confidence": 0-100,
  "assessment": "Analisis 2-3 kalimat dalam Bahasa Indonesia tentang pola trading whale ini",
  "suggestion": "Rekomendasi untuk kamu: ikut/tidak, beli/jual, dll dalam 1-2 kalimat",
  "recent_mood": "bullish | bearish | neutral",
  "top_token": "Token yang paling sering ditradingkan"
}}
Hanya JSON, tanpa teks lain."""
        result = self._call_llm(prompt)
        if result:
            return result
        return {"whale_type": "unknown", "confidence": 50, "assessment": "Gagal analisis", "suggestion": "Coba refresh", "recent_mood": "neutral", "top_token": ""}
