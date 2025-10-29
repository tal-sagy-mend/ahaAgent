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
    idea_name = idea.get("name") or "Unnamed idea"
    base_url = os.environ.get("AHA_BASE_URL", "").rstrip("/")
    
    # Try to get the URL from Aha first, then construct it
    idea_url = idea.get("url")
    if not idea_url and base_url:
        # Try reference_num first (e.g., "MEND-I-123"), then fall back to id
        ref = idea.get("reference_num") or idea.get("reference")
        if ref:
            idea_url = f"{base_url}/ideas/ideas/{ref}"
        else:
            idea_id = idea.get("id")
            if idea_id:
                idea_url = f"{base_url}/ideas/{idea_id}"
    
    print(f"[DEBUG] Constructed URL: {idea_url}")

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
    def section(label: str, text: str, mark_missing: bool = True):
        if not text or (text.strip() == "" and mark_missing):
            return {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{label}:*\n_[Not provided]_ ⚠️"}
            }
        return {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{label}:*\n{_shorten(text)}"}
        }

    # Slack message layout (rich version)
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"⚠️ Needs review: {idea_name}", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Idea:*\n<{idea_url}|Open in Aha!>" if idea_url else "*Idea:*\nNo URL available"},
                {"type": "mrkdwn", "text": f"*Customer:*\n{customer or '—'}"},
            ],
        },
        {"type": "divider"},
    ]

    # Add the context sections dynamically
    for lbl, val in (
        ("Current behavior (What is the challenge?)", current),
        ("Impact (How this affects workflow/goals)", impact),
        ("Requested behavior (Improvement needed)", request_txt),
        ("Additional description", desc),
    ):
        sec = section(lbl, val)
        blocks.append(sec)

    # Add the critique
    blocks += [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🤖 AI Product Manager Review*\n{_shorten(critique, 2800)}"},
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
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")  # Options: gpt-4o, gpt-4-turbo, o1-preview
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
# Analyze idea quality using OpenAI
# ------------------------------------------------------------

def analyze_idea_quality(idea: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Analyzes if an idea is well-described. Returns None if idea is good,
    or a dict with 'needs_improvement': True and 'critique': str if it needs work.
    """
    if not OPENAI_API_KEY:
        return None
    
    # Extract fields
    cf = idea.get("custom_fields", {}) or {}
    name = idea.get("name") or "Unnamed idea"
    current = _strip_html(idea.get("current_behavior") or cf.get("current_behavior") or "")
    impact = _strip_html(idea.get("impact") or cf.get("impact") or "")
    requested = _strip_html(idea.get("requested_behavior") or cf.get("requested_behavior") or "")
    description = _strip_html(idea.get("description") or "")
    
    # Check if key fields are missing or too short
    fields_provided = {
        "current_behavior": len(current.strip()) > 20,
        "impact": len(impact.strip()) > 20,
        "requested_behavior": len(requested.strip()) > 20,
    }
    
    # Build context for analysis
    content = f"""Title: {name}

Current Behavior (What is the challenge?):
{current or "[NOT PROVIDED]"}

Impact (How this affects workflow/goals):
{impact or "[NOT PROVIDED]"}

Requested Behavior (Improvement or feature needed):
{requested or "[NOT PROVIDED]"}

Additional Description:
{description or "[NOT PROVIDED]"}"""

    prompt = """You are an experienced product manager reviewing an idea submission. Your job is to determine if the submitter has:

1. Clearly described the PROBLEM/PAIN they're experiencing (good)
2. Explained the IMPACT and why it matters (good)
3. Focused on the USE CASE rather than prescribing a specific solution (good)

vs.

1. Jumped to describing HOW to implement a solution (bad)
2. Dictated technical details instead of explaining the customer pain (bad)
3. Left key fields empty or vague (bad)

Analyze the idea below and determine if it needs improvement.

Required fields that should be well-described:
- Current Behavior: What challenge/problem exists today?
- Impact: How does this affect their workflow, goals, or business?
- Requested Behavior: What improvement would help (focus on outcome, not implementation)

RED FLAGS to watch for:
- Solution-focused language ("add a button", "create a new API", "implement X technology")
- Missing or vague problem description
- No clear impact or use case
- Focus on HOW instead of WHY

{content}

Respond in JSON format:
{{
  "needs_improvement": true/false,
  "issues_found": ["issue1", "issue2", ...],
  "critique": "A constructive message to the submitter if needs_improvement=true, or empty string if false"
}}

The critique should be friendly, specific, and guide them to focus on customer pain, impact, and use cases."""

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": "You are a product manager reviewing idea submissions. Respond only with valid JSON."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.3,
                "response_format": {"type": "json_object"}
            },
            timeout=30,
        )
        
        if response.status_code != 200:
            print(f"OpenAI API error: {response.status_code} - {response.text}")
            return None
            
        result = response.json()
        content = result["choices"][0]["message"]["content"]
        analysis = json.loads(content)
        
        return analysis if analysis.get("needs_improvement") else None
        
    except Exception as e:
        print(f"Error analyzing idea: {e}")
        return None

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
    
    # Debug logging
    print(f"[DEBUG] Received webhook data keys: {data.keys()}")
    print(f"[DEBUG] Event type: {data.get('event')}")
    
    # Check if this is a creation event
    event_type = data.get("event")
    if event_type and event_type not in ("create", "created"):
        return jsonify({"status": "skipped", "reason": f"ignoring event type: {event_type}"})
    
    idea = data.get("idea") or data
    
    # Debug logging for idea fields
    print(f"[DEBUG] Idea keys: {idea.keys() if isinstance(idea, dict) else 'not a dict'}")
    print(f"[DEBUG] Idea ID: {idea.get('id')}")
    print(f"[DEBUG] Reference num: {idea.get('reference_num')}")
    print(f"[DEBUG] Reference: {idea.get('reference')}")
    print(f"[DEBUG] URL from Aha: {idea.get('url')}")
    
    idea_id = str(idea.get("id") or idea.get("reference_num") or idea.get("reference"))

    if not idea_id:
        return ("no idea id", 400)

    # Analyze the idea quality using AI
    analysis = analyze_idea_quality(idea)
    
    # Only notify if the idea needs improvement
    if not analysis or not analysis.get("needs_improvement"):
        return jsonify({
            "status": "ok",
            "idea_id": idea_id,
            "action": "no_action_needed",
            "message": "Idea is well-described"
        })
    
    # Get the AI-generated critique
    critique = analysis.get("critique", "")
    issues = analysis.get("issues_found", [])
    
    # Format the critique for Aha comment
    draft_body = f"[DRAFT – Tal review required]\n\n{critique}"
    
    if issues:
        draft_body += f"\n\n**Issues identified:**\n" + "\n".join(f"• {issue}" for issue in issues)

    try:
        aha_post_private_comment(idea_id, draft_body)
        blocks = build_slack_blocks(idea, critique)
        slack_notify(
            text=f"⚠️ Idea needs review: {idea.get('name') or 'Unnamed idea'}",
            blocks=blocks,
        )
    except Exception as e:
        slack_notify(text=f"⚠️ Failed to add draft private note for idea {idea_id}: {e}")

    return jsonify({
        "status": "ok",
        "idea_id": idea_id,
        "action": "notified",
        "issues": issues
    })

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
