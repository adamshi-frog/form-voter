#!/usr/bin/env python3
"""Google Form vote bot — repeatedly submits a chosen answer to a Google Form."""

import argparse
import random
import re
import sys
import time

import requests
from bs4 import BeautifulSoup


def parse_form(url: str) -> dict:
    """Fetch the form HTML and extract submission URL, entry IDs, and options."""
    # Normalize to viewform URL
    match = re.search(r"(https://docs\.google\.com/forms/d/e/[^/]+)", url)
    if not match:
        print("Error: Could not parse Google Form ID from URL.")
        sys.exit(1)

    base_url = match.group(1)
    view_url = base_url + "/viewform"
    submit_url = base_url + "/formResponse"

    resp = requests.get(view_url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Google Forms embeds structured data in a script tag as FB_PUBLIC_LOAD_DATA_
    # We parse the HTML for data-params attributes on question containers
    questions = []

    # Method 1: Parse from the JS data blob (FB_PUBLIC_LOAD_DATA_)
    script_text = resp.text
    fb_match = re.search(r"FB_PUBLIC_LOAD_DATA_\s*=\s*(.*?);\s*</script>", script_text, re.DOTALL)
    if fb_match:
        import json
        try:
            raw = fb_match.group(1)
            data = json.loads(raw)
            # data[1][1] contains the list of questions
            for item in data[1][1]:
                if not isinstance(item, list) or len(item) < 5:
                    continue
                title = item[1] if len(item) > 1 else "Unknown"
                entry_id = None
                options = []
                # item[4] contains answer metadata
                if item[4] and isinstance(item[4], list):
                    for answer_group in item[4]:
                        if isinstance(answer_group, list) and len(answer_group) > 0:
                            # answer_group[0] contains the entry ID and options
                            if isinstance(answer_group[0], list) and len(answer_group[0]) > 0:
                                entry_id = answer_group[0][0]
                            # Options are in answer_group[1]
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

    # Method 2: Fallback — scrape input/select elements
    if not questions:
        for inp in soup.find_all("input", attrs={"name": re.compile(r"^entry\.")}):
            entry_id = inp["name"]
            # Try to find the question label nearby
            parent = inp.find_parent("div", class_=re.compile(r"freebirdFormview"))
            title = parent.get_text(strip=True)[:80] if parent else entry_id
            questions.append({
                "title": title,
                "entry_id": entry_id,
                "options": [],
            })

    if not questions:
        print("Error: Could not find any questions in the form.")
        print("Make sure the form URL is correct and the form doesn't require sign-in.")
        sys.exit(1)

    return {"submit_url": submit_url, "questions": questions}


def select_answers(questions: list) -> dict:
    """Interactively prompt the user to pick an answer for each question."""
    answers = {}
    for i, q in enumerate(questions, 1):
        print(f"\nQuestion {i}: {q['title']}")
        if q["options"]:
            for j, opt in enumerate(q["options"], 1):
                print(f"  {j}. {opt}")
            while True:
                choice = input(f"Select option (1-{len(q['options'])}): ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(q["options"]):
                    answers[q["entry_id"]] = q["options"][int(choice) - 1]
                    break
                print("Invalid choice, try again.")
        else:
            val = input("Enter your answer: ").strip()
            answers[q["entry_id"]] = val
    return answers


def submit_votes(submit_url: str, answers: dict, count: int, delay_min: float, delay_max: float):
    """Submit the form `count` times with random delays."""
    success = 0
    for i in range(1, count + 1):
        try:
            resp = requests.post(submit_url, data=answers)
            if resp.status_code == 200:
                success += 1
                print(f"[{i}/{count}] Submitted successfully")
            else:
                print(f"[{i}/{count}] Failed (HTTP {resp.status_code})")
        except requests.RequestException as e:
            print(f"[{i}/{count}] Error: {e}")

        if i < count:
            delay = random.uniform(delay_min, delay_max)
            time.sleep(delay)

    print(f"\nDone. {success}/{count} votes submitted successfully.")


def main():
    parser = argparse.ArgumentParser(description="Google Form vote bot")
    parser.add_argument("--url", required=True, help="Google Form URL")
    parser.add_argument("--count", type=int, default=10, help="Number of votes (default: 10)")
    parser.add_argument("--delay-min", type=float, default=1.0, help="Min delay between votes in seconds (default: 1)")
    parser.add_argument("--delay-max", type=float, default=3.0, help="Max delay between votes in seconds (default: 3)")
    args = parser.parse_args()

    print(f"Fetching form: {args.url}")
    form_data = parse_form(args.url)

    print(f"\nFound {len(form_data['questions'])} question(s):")
    answers = select_answers(form_data["questions"])

    print(f"\nSubmitting {args.count} votes to: {form_data['submit_url']}")
    print(f"Delay: {args.delay_min}-{args.delay_max}s between requests\n")
    submit_votes(form_data["submit_url"], answers, args.count, args.delay_min, args.delay_max)


if __name__ == "__main__":
    main()
