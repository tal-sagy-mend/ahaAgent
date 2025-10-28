from typing import Any, Dict, Optional
import os
import json
import re
from flask import Flask, request, jsonify
import requests

# ------------------------------------------------------------
# Helpers for cleaning and shortening text
# ------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Remove HTML tags from text."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text)

def _shorten(s: str, limit: int = 2500) -> str:
    """Slack block text has a 3000-char limit; leave room for label."""
    if not s:
        return ""
    return s if len(s) <= limit else s[:limit] + "…"

# ------------------------------------------------------------
# Build Slack message blocks
# ------------------------------------------------------------

def build_slack_blocks(idea: dict, critique: str) -> list:
    # Core identity
    idea_id = idea.get("reference_num") or idea.get("reference") or idea.get("id") or "unknown"
    idea_name = idea.get("name") or "Unnamed idea"
    base_url = os.environ.get("AHA_BASE_URL", "").rstrip("/")
    idea_url = idea.get("url") or (f"{base_url}/ideas/{idea_id}" if base_url else "")

    # Common fields shown on your form
    cf = idea.get("custom_fields", {}) or {}
    current = _strip_html(idea.get("current_behavior") or cf.get("current_behavior"))
    impact = _strip_html(idea.get("impact") or cf.get("impact"))
    request_txt = _strip_html(idea.get("requested_behavior") or cf.get("requested_behavior"))
    desc = _strip_html(idea.get("description"))

    # Customer (uses your existing custom field if present)
    customer = (idea.get("customer_name") or
                cf.get("customer_name") or
                cf.get("organization") or
                cf.get("organization_name") or "")

    # Helper for consistent section formatting
    def section(label: str, text: str):
        if not text:
            return None
        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{label}:*\n{_shorten(text)}"}
        }

    # Slack message layout (rich version)
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{idea_name} ({customer or '—'})", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Idea:*\n<{idea_url or (base_url + '/ideas/' + str(idea_id))}|Open in Aha!>"},
                {"type": "mrkdwn", "text": f"*Customer:*\n{customer or '—'}"},
            ],
        },
        {"type": "divider"},
    ]

    # Add the context sections dynamically
    for lbl, val in (
        ("Current behavior", current),
        ("Impact", impact),
        ("Requested behavior", request_txt),
        ("Full description", desc),
    ):
        sec = section(lbl, val)
        if sec:
            blocks.append(sec)

    # Add the critique
    blocks += [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Draft critique (for your review)*\n```{_shorten(critique)}```"},
        },
    ]

    return blocks

# ------------------------------------------------------------
# Flask setup and core app
# ------------------------------------------------------------

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

# ------------------------------------------------------------
# Slack notification
# ------------------------------------------------------------

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

# ------------------------------------------------------------
# Generate critique comment using OpenAI
# ------------------------------------------------------------

def gen_critique_comment(idea: Dict[str, Any]) -> str:
    title = idea.get("name") or idea.get("title") or "this idea"
    description = (idea.get("description") or "").strip()
    current_behavior = _strip_html(description)
    impact = _strip_html(idea.get("impact"))
    requested = _strip_html(idea.get("requested_behavior"))

    critique = (
        f"Thanks for submitting this idea! Could you describe the customer pain and measurable impact "
        f"in more detail—what currently happens, how often, and what the business effect is? "
        f"This will help us prioritize and design the right solution."
    )
    return critique

# ------------------------------------------------------------
# Post a comment back to Aha!
# ------------------------------------------------------------

def aha_post_private_comment(idea_id, body):
    payload = {"comment": {"body": body, "visibility": "private"}}
    resp = SESSION.post(f"{AHA_BASE_URL}/api/v1/ideas/{idea_id}/comments", json=payload)
    if resp.status_code >= 300:
        print(f"Failed to post to Aha! ({resp.status_code}): {resp.text}")
    return resp.json() if resp.ok else {}

# ------------------------------------------------------------
# Webhook endpoint
# ------------------------------------------------------------

@app.route("/aha/webhook", methods=["POST", "HEAD", "GET"])
def aha_webhook():
    # Health check
    if request.method in ("HEAD", "GET"):
        return ("", 200)

    data = request.get_json(force=True, silent=True) or {}
    idea = data.get("idea") or data
    idea_id = str(idea.get("id") or idea.get("reference_num") or idea.get("reference"))

    if not idea_id:
        return ("no idea id", 400)

    critique = gen_critique_comment(idea)
    draft_body = f"[DRAFT – Tal review required]\n\n{critique}"

    try:
        aha_post_private_comment(idea_id, draft_body)
        blocks = build_slack_blocks(idea, critique)
        slack_notify(
            text=f"Draft note for {idea.get('name') or 'idea'}",
            blocks=blocks,
        )
    except Exception as e:
        slack_notify(text=f"⚠️ Failed to add draft private note for idea {idea_id}: {e}")

    return jsonify({"status": "ok", "idea_id": idea_id})

# ------------------------------------------------------------
# Health check endpoint
# ------------------------------------------------------------

@app.route("/health")
def health():
    return {"ok": True}

# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
