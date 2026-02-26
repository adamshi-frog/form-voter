#!/usr/bin/env python3
"""Web frontend for the Google Form vote bot."""

import json
import re
import random
import time
import threading

from flask import Flask, render_template_string, request, jsonify, Response
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
]

def random_headers():
    """Generate randomized browser-like headers for each request."""
    ua = random.choice(USER_AGENTS)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": random.choice(["en-US,en;q=0.9", "en-GB,en;q=0.9", "en-US,en;q=0.5"]),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://docs.google.com/",
    }

# --- Reuse parsing logic from form_voter.py ---

def parse_form(url: str) -> dict:
    match = re.search(r"(https://docs\.google\.com/forms/d/e/[^/]+)", url)
    if not match:
        raise ValueError("Could not parse Google Form ID from URL.")

    base_url = match.group(1)
    view_url = base_url + "/viewform"
    submit_url = base_url + "/formResponse"

    resp = requests.get(view_url)
    resp.raise_for_status()

    # Extract hidden fields Google expects (fbzx token, etc.)
    hidden_fields = {}
    soup_full = BeautifulSoup(resp.text, "html.parser")
    for inp in soup_full.find_all("input", {"type": "hidden"}):
        name = inp.get("name")
        val = inp.get("value", "")
        if name:
            hidden_fields[name] = val
    # Also grab fbzx from the data blob if not in hidden inputs
    fbzx_match = re.search(r'"fbzx":"([^"]+)"', resp.text)
    if fbzx_match and "fbzx" not in hidden_fields:
        hidden_fields["fbzx"] = fbzx_match.group(1)

    questions = []
    fb_match = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", resp.text, re.DOTALL)
    if fb_match:
        try:
            data = json.loads(fb_match.group(1))
            for item in data[1][1]:
                if not isinstance(item, list) or len(item) < 5:
                    continue
                title = item[1] if len(item) > 1 else "Unknown"
                entry_id = None
                options = []
                if item[4] and isinstance(item[4], list):
                    for answer_group in item[4]:
                        if isinstance(answer_group, list) and len(answer_group) > 0:
                            # entry ID can be a bare int or wrapped in a list
                            if isinstance(answer_group[0], list) and len(answer_group[0]) > 0:
                                entry_id = answer_group[0][0]
                            elif isinstance(answer_group[0], (int, float)):
                                entry_id = int(answer_group[0])
                            if len(answer_group) > 1 and isinstance(answer_group[1], list):
                                for opt in answer_group[1]:
                                    if isinstance(opt, list) and len(opt) > 0:
                                        options.append(opt[0])
                if entry_id is not None:
                    questions.append({
                        "title": title,
                        "entry_id": f"entry.{entry_id}",
                        "options": options,
                    })
        except (json.JSONDecodeError, IndexError, TypeError):
            pass

    if not questions:
        soup = BeautifulSoup(resp.text, "html.parser")
        for inp in soup.find_all("input", attrs={"name": re.compile(r"^entry\.")}):
            entry_id = inp["name"]
            parent = inp.find_parent("div", class_=re.compile(r"freebirdFormview"))
            title = parent.get_text(strip=True)[:80] if parent else entry_id
            questions.append({"title": title, "entry_id": entry_id, "options": []})

    if not questions:
        raise ValueError("Could not find any questions. Check the URL and that the form doesn't require sign-in.")

    # Include standard fields Google Forms expects
    hidden_fields.setdefault("fvv", "1")
    hidden_fields.setdefault("pageHistory", "0")

    return {"submit_url": submit_url, "questions": questions, "hidden_fields": hidden_fields}


# --- Submission verification ---

CONFIRM_INDICATORS = [
    "freebirdFormviewerViewResponseConfirmationMessage",
    "Your response has been recorded",
    "Thanks for your response",
]

def check_confirmed(html: str) -> bool:
    """Check if the Google Forms response HTML contains a real confirmation."""
    for indicator in CONFIRM_INDICATORS:
        if indicator in html:
            return True
    return False


# --- API routes ---

@app.route("/api/parse", methods=["POST"])
def api_parse():
    url = request.json.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    try:
        form_data = parse_form(url)
        return jsonify(form_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/test", methods=["POST"])
def api_test():
    """Submit a single vote and return detailed verification info."""
    data = request.json
    submit_url = data.get("submit_url")
    answers = data.get("answers", {})
    hidden_fields = data.get("hidden_fields", {})

    if not submit_url or not answers:
        return jsonify({"error": "Missing submit_url or answers"}), 400

    try:
        payload = {**hidden_fields, **answers}
        resp = requests.post(submit_url, data=payload, headers=random_headers())
        confirmed = check_confirmed(resp.text)

        # Extract the confirmation message text if present
        confirm_msg = None
        soup = BeautifulSoup(resp.text, "html.parser")
        el = soup.find(class_="freebirdFormviewerViewResponseConfirmationMessage")
        if el:
            confirm_msg = el.get_text(strip=True)

        return jsonify({
            "http_status": resp.status_code,
            "confirmed": confirmed,
            "confirm_message": confirm_msg,
            "payload_sent": answers,
        })
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/vote", methods=["POST"])
def api_vote():
    data = request.json
    submit_url = data.get("submit_url")
    answers = data.get("answers", {})
    hidden_fields = data.get("hidden_fields", {})
    count = int(data.get("count", 10))
    delay_min = float(data.get("delay_min", 1.0))
    delay_max = float(data.get("delay_max", 3.0))

    if not submit_url or not answers:
        return jsonify({"error": "Missing submit_url or answers"}), 400

    payload = {**hidden_fields, **answers}

    def generate():
        success = 0
        failed = 0
        backoff = 0  # extra delay added when rate limited
        for i in range(1, count + 1):
            try:
                resp = requests.post(submit_url, data=payload, headers=random_headers())
                confirmed = check_confirmed(resp.text)
                if resp.status_code == 200 and confirmed:
                    success += 1
                    backoff = max(0, backoff - 1)  # ease off backoff on success
                    yield f"data: {json.dumps({'i': i, 'total': count, 'status': 'ok', 'success': success, 'failed': failed})}\n\n"
                elif resp.status_code == 429 or (resp.status_code == 200 and not confirmed):
                    failed += 1
                    backoff = min(backoff + 3, 30)  # increase backoff, cap at 30s
                    status = 'rate_limited' if resp.status_code == 429 else 'rejected'
                    yield f"data: {json.dumps({'i': i, 'total': count, 'status': status, 'success': success, 'failed': failed, 'backoff': backoff})}\n\n"
                else:
                    failed += 1
                    yield f"data: {json.dumps({'i': i, 'total': count, 'status': 'fail', 'code': resp.status_code, 'success': success, 'failed': failed})}\n\n"
            except requests.RequestException as e:
                failed += 1
                backoff = min(backoff + 2, 30)
                yield f"data: {json.dumps({'i': i, 'total': count, 'status': 'error', 'message': str(e), 'success': success, 'failed': failed})}\n\n"

            if i < count:
                delay = random.uniform(delay_min, delay_max) + backoff
                time.sleep(delay)

        yield f"data: {json.dumps({'done': True, 'success': success, 'failed': failed, 'total': count})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# --- Frontend ---

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Form Voter</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; display: flex; justify-content: center; padding: 40px 20px; }
  .container { max-width: 540px; width: 100%; }
  h1 { font-size: 24px; font-weight: 600; margin-bottom: 32px; color: #fff; }
  label { display: block; font-size: 13px; font-weight: 500; color: #888; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 0.5px; }
  input[type="text"], input[type="number"] { width: 100%; padding: 10px 12px; background: #161616; border: 1px solid #2a2a2a; border-radius: 8px; color: #e0e0e0; font-size: 15px; outline: none; transition: border-color 0.2s; }
  input:focus { border-color: #555; }
  .field { margin-bottom: 20px; }
  button { padding: 10px 20px; border: none; border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; transition: opacity 0.15s; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-primary { background: #fff; color: #000; }
  .btn-primary:hover:not(:disabled) { opacity: 0.85; }
  .btn-danger { background: #dc2626; color: #fff; }
  .question { background: #161616; border: 1px solid #2a2a2a; border-radius: 10px; padding: 16px; margin-bottom: 14px; }
  .question h3 { font-size: 15px; font-weight: 500; margin-bottom: 10px; color: #fff; }
  .option { display: flex; align-items: center; gap: 8px; padding: 7px 0; cursor: pointer; font-size: 14px; }
  .option input[type="radio"] { accent-color: #fff; }
  .option.selected { color: #fff; font-weight: 500; }
  .row { display: flex; gap: 12px; }
  .row .field { flex: 1; }
  #status { margin-top: 16px; }
  .log { background: #161616; border: 1px solid #2a2a2a; border-radius: 10px; padding: 14px; max-height: 260px; overflow-y: auto; font-family: 'SF Mono', Monaco, monospace; font-size: 13px; line-height: 1.7; }
  .log .ok { color: #22c55e; }
  .log .fail { color: #ef4444; }
  .log .rejected { color: #f59e0b; }
  .log .done { color: #3b82f6; font-weight: 600; }
  .test-result { background: #161616; border: 1px solid #2a2a2a; border-radius: 10px; padding: 14px; margin-top: 12px; font-size: 14px; }
  .test-result.pass { border-color: #22c55e; }
  .test-result.fail { border-color: #ef4444; }
  .test-result .label { font-weight: 600; margin-bottom: 6px; }
  .test-result .detail { color: #888; font-size: 13px; font-family: 'SF Mono', Monaco, monospace; }
  .btn-row { display: flex; gap: 10px; }
  .btn-secondary { background: #2a2a2a; color: #e0e0e0; }
  .btn-secondary:hover:not(:disabled) { background: #333; }
  .progress-bar { height: 4px; background: #2a2a2a; border-radius: 2px; margin-bottom: 12px; overflow: hidden; }
  .progress-bar .fill { height: 100%; background: #fff; transition: width 0.3s; width: 0%; }
  .error { color: #ef4444; font-size: 14px; margin-top: 8px; }
  .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #555; border-top-color: #fff; border-radius: 50%; animation: spin 0.6s linear infinite; margin-right: 8px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }
  #questions-section, #vote-section, #status { display: none; }
</style>
</head>
<body>
<div class="container">
  <h1>Form Voter</h1>

  <div id="url-section">
    <div class="field">
      <label>Google Form URL</label>
      <input type="text" id="url" placeholder="https://docs.google.com/forms/d/e/...">
    </div>
    <button class="btn-primary" id="fetch-btn" onclick="fetchForm()">Load Form</button>
    <div id="fetch-error" class="error"></div>
  </div>

  <div id="questions-section"></div>

  <div id="vote-section">
    <div class="row">
      <div class="field">
        <label>Votes</label>
        <input type="number" id="count" value="10" min="1" max="10000">
      </div>
      <div class="field">
        <label>Min Delay (s)</label>
        <input type="number" id="delay-min" value="1" min="0" step="0.5">
      </div>
      <div class="field">
        <label>Max Delay (s)</label>
        <input type="number" id="delay-max" value="3" min="0" step="0.5">
      </div>
    </div>
    <div class="btn-row">
      <button class="btn-secondary" id="test-btn" onclick="testVote()">Test 1 Vote</button>
      <button class="btn-primary" id="vote-btn" onclick="startVoting()">Start Voting</button>
    </div>
    <div id="test-result"></div>
  </div>

  <div id="status">
    <div class="progress-bar"><div class="fill" id="progress-fill"></div></div>
    <div class="log" id="log"></div>
  </div>
</div>

<script>
let formData = null;

async function fetchForm() {
  const url = document.getElementById('url').value.trim();
  const btn = document.getElementById('fetch-btn');
  const err = document.getElementById('fetch-error');
  err.textContent = '';
  if (!url) { err.textContent = 'Paste a URL first.'; return; }

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Loading...';

  try {
    const res = await fetch('/api/parse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    formData = data;
    renderQuestions(data.questions);
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Load Form';
  }
}

function renderQuestions(questions) {
  const section = document.getElementById('questions-section');
  section.style.display = 'block';
  section.innerHTML = questions.map((q, qi) => `
    <div class="question">
      <h3>${q.title}</h3>
      ${q.options.length ? q.options.map((opt, oi) => `
        <label class="option">
          <input type="radio" name="q${qi}" value="${oi}">
          ${opt}
        </label>
      `).join('') : `<input type="text" class="freetext" data-qi="${qi}" placeholder="Type your answer">`}
    </div>
  `).join('');
  document.getElementById('vote-section').style.display = 'block';
}

function getAnswers() {
  const answers = {};
  formData.questions.forEach((q, qi) => {
    if (q.options.length) {
      const checked = document.querySelector(`input[name="q${qi}"]:checked`);
      if (checked) answers[q.entry_id] = q.options[parseInt(checked.value)];
    } else {
      const input = document.querySelector(`.freetext[data-qi="${qi}"]`);
      if (input && input.value.trim()) answers[q.entry_id] = input.value.trim();
    }
  });
  return answers;
}

async function testVote() {
  const answers = getAnswers();
  if (Object.keys(answers).length === 0) { alert('Select an answer first.'); return; }

  const btn = document.getElementById('test-btn');
  const result = document.getElementById('test-result');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Testing...';
  result.innerHTML = '';

  try {
    const res = await fetch('/api/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ submit_url: formData.submit_url, answers, hidden_fields: formData.hidden_fields || {} })
    });
    const d = await res.json();
    if (d.error) {
      result.innerHTML = `<div class="test-result fail"><div class="label">Error</div><div class="detail">${d.error}</div></div>`;
    } else if (d.confirmed) {
      result.innerHTML = `<div class="test-result pass"><div class="label">Vote confirmed</div><div class="detail">HTTP ${d.http_status} — "${d.confirm_message || 'Response recorded'}"<br>Payload: ${JSON.stringify(d.payload_sent)}</div></div>`;
    } else {
      result.innerHTML = `<div class="test-result fail"><div class="label">Vote NOT confirmed</div><div class="detail">HTTP ${d.http_status} — No confirmation message found in response.<br>The form may require sign-in, or the entry IDs/values may be wrong.<br>Payload sent: ${JSON.stringify(d.payload_sent)}</div></div>`;
    }
  } catch (e) {
    result.innerHTML = `<div class="test-result fail"><div class="label">Request failed</div><div class="detail">${e.message}</div></div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Test 1 Vote';
  }
}

function startVoting() {
  const answers = getAnswers();
  if (Object.keys(answers).length === 0) { alert('Select an answer first.'); return; }

  const count = parseInt(document.getElementById('count').value) || 10;
  const delayMin = parseFloat(document.getElementById('delay-min').value) || 1;
  const delayMax = parseFloat(document.getElementById('delay-max').value) || 3;

  document.getElementById('vote-btn').disabled = true;
  document.getElementById('test-btn').disabled = true;
  document.getElementById('status').style.display = 'block';
  const log = document.getElementById('log');
  const fill = document.getElementById('progress-fill');
  log.innerHTML = '';

  fetch('/api/vote', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      submit_url: formData.submit_url,
      answers, hidden_fields: formData.hidden_fields || {},
      count, delay_min: delayMin, delay_max: delayMax
    })
  }).then(res => {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    function read() {
      reader.read().then(({done, value}) => {
        if (done) return;
        buffer += decoder.decode(value, {stream: true});
        const lines = buffer.split('\\n');
        buffer = lines.pop();
        lines.forEach(line => {
          if (!line.startsWith('data: ')) return;
          const d = JSON.parse(line.slice(6));
          if (d.done) {
            log.innerHTML += `<div class="done">Done: ${d.success} confirmed / ${d.failed} failed / ${d.total} total</div>`;
            document.getElementById('vote-btn').disabled = false;
            document.getElementById('test-btn').disabled = false;
          } else {
            const pct = (d.i / d.total * 100).toFixed(1);
            fill.style.width = pct + '%';
            let cls = 'fail';
            let msg = 'Failed';
            if (d.status === 'ok') { cls = 'ok'; msg = 'Confirmed'; }
            else if (d.status === 'rate_limited') { cls = 'rejected'; msg = `Rate limited — backing off +${d.backoff}s`; }
            else if (d.status === 'rejected') { cls = 'rejected'; msg = 'Rejected (not counted)'; }
            else if (d.code) { msg = 'HTTP ' + d.code; }
            else if (d.message) { msg = d.message; }
            log.innerHTML += `<div class="${cls}">[${d.i}/${d.total}] ${msg}</div>`;
          }
          log.scrollTop = log.scrollHeight;
        });
        read();
      });
    }
    read();
  });
}
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)


if __name__ == "__main__":
    app.run(debug=True, port=5050)
