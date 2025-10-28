from typing import Any, Dict, Optional
import os
import json
import re
from flask import Flask, request, jsonify
import requests

try:
    import openai
    OPENAI_READY = True
except Exception:
    OPENAI_READY = False

app = Flask(__name__)

AHA_BASE_URL = os.environ.get("AHA_BASE_URL", "").rstrip("/")
AHA_API_TOKEN = os.environ.get("AHA_API_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CUSTOMER_FIELD_API_KEY = os.environ.get("CUSTOMER_FIELD_API_KEY", "")

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {AHA_API_TOKEN}",
    "Content-Type": "application/json",
    "Accept": "application/json",
})

def slack_notify(text: str, blocks: Optional[list] = None) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    payload: Dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    try:
        requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass

def gen_critique_comment(idea: Dict[str, Any]) -> str:
    title = idea.get("name") or idea.get("title") or "this idea"
    description = (idea.get("description") or "").strip()
    current_behavior = _extract_section(description, "Current Behavior")
    impact = _extract_section(description, "What is the impact")
    requested = _extract_section(description, "Requested Behavior")

    prompt = f"""
You review Aha! feature requests. Draft a concise, professional private note (max 4 sentences)
asking the author for clarifications. Focus on:
- customer pain/motivation, measurable impact
- avoid solution bias
Title: {title}
Current Behavior: {current_behavior}
Impact: {impact}
Requested Behavior: {requested}
Description: {description[:2000]}
""".strip()

    if OPENAI_READY and OPENAI_API_KEY:
        try:
            openai.api_key = OPENAI_API_KEY
            resp = openai.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a precise, helpful product manager."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=200,
            )
            return resp["choices"][0]["message"]["content"].strip()
        except Exception:
            pass

    return (
        "Thanks for submitting this idea! Could you describe the customer pain and measurable "
        "impact in more detail—what currently happens, how often, and what the business effect is? "
        "This will help us prioritize and design the right solution."
    )

def _extract_section(text: str, heading: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text or "")
    m = re.search(rf"{heading}\\s*:?\\s*(.+)", clean, re.IGNORECASE)
    return (m.group(1).strip() if m else "")[:500]

def aha_post_private_comment(idea_id: str, body: str):
    payloads = [
        {"comment": {"body": body, "visibility": "private"}},
        {"comment": {"body": body, "private": True}},
    ]
    for payload in payloads:
        r = SESSION.post(f"{AHA_BASE_URL}/api/v1/ideas/{idea_id}/comments", json=payload)
        if r.status_code < 300:
            return r.json()
    return {}

def extract_customer_from_idea(idea: Dict[str, Any]) -> str:
    for pv in (idea.get("proxy_votes") or []):
        org = (pv.get("organization") or {}).get("name") or pv.get("organization_name")
        if org:
            return org
    return ""

@app.route("/aha/webhook", methods=["POST", "HEAD", "GET"])
def aha_webhook():
    # Allow Aha! to verify the webhook and run quick health checks
    if request.method in ("HEAD", "GET"):
        return ("", 200)

    data = request.get_json(force=True, silent=True) or {}
    idea = data.get("idea") or data
    idea_id = str(idea.get("id") or idea.get("reference_num") or idea.get("reference"))
    if not idea_id:
        return ("no idea id", 400)
    critique = gen_critique_comment(idea)
    draft_body = f"[DRAFT – Tal review required]\\n\\n{critique}"
    try:
        aha_post_private_comment(idea_id, draft_body)
        slack_notify(
            text=f"Draft private note created for idea {idea.get('name')}",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Idea:* {idea.get('name')}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"```{critique}```"}},
            ],
        )
    except Exception as e:
        slack_notify(text=f"Failed to add draft private note for idea {idea_id}: {e}")
    return jsonify({"status": "ok", "idea_id": idea_id})

@app.route("/health")
def health():
    return {"ok": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
