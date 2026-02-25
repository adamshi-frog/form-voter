# Form Voter

Google Form vote bot with a CLI and web UI. Parses any public Google Form, lets you pick answers, and submits multiple votes with configurable delays.

## Setup

```bash
pip install -r requirements.txt
```

## Web UI

```bash
python form_voter_web.py
```

Open http://localhost:5050. Paste a Google Form URL, select your answers, test a single vote to verify it's being counted, then bulk submit.

## CLI

```bash
python form_voter.py --url "https://docs.google.com/forms/d/e/..." --count 50 --delay-min 1 --delay-max 3
```

| Flag | Default | Description |
|------|---------|-------------|
| `--url` | required | Google Form URL |
| `--count` | 10 | Number of votes |
| `--delay-min` | 1.0 | Min seconds between votes |
| `--delay-max` | 3.0 | Max seconds between votes |

## Limitations

- Only works with public forms (no Google sign-in required)
- Forms with CAPTCHA or anti-bot measures will block submissions
