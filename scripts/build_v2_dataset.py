#!/usr/bin/env python3
"""build_v2_dataset.py — build the v2 retraining dataset for dourmouse.

Produces (from training_data/):

  v2_train.jsonl   chat-format rows for SFT:
                   - 849 real dispatcher pairs (943 minus the 94 held out),
                     system = the PRODUCTION dispatch prompt
                     (dourmouse.dispatch.system_message(registry))
                   - ~58 pivot action-label rows: small-business workflow
                     requests answered with the strict action contract
                     (assistant_message / action / needs_confirmation /
                     confidence) covering all 13 action types, so a retrained
                     model also speaks the commercial-phase structured format.
  v2_valid.jsonl   the 94 held-out pairs (conversation-stratified, seed 42)
                   in the same chat format — the eval set for the GPU run.

The split matches training_data/held_out.jsonl (exclusion by conversation
uuid, so the valid set never leaks into train). Seeded shuffle (42) for a
reproducible row order.

Usage:
  .venv/bin/python scripts/build_v2_dataset.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dourmouse.dispatch import system_message
from dourmouse.general_roster import build_general_registry

DATA = Path(__file__).resolve().parent.parent / "training_data"

PIVOT_SYSTEM = (
    "You are Dourmouse, a small-business workflow model. "
    "Return only valid JSON matching the Dourmouse output schema: "
    "{\"assistant_message\": string, \"action\": {\"type\": string, "
    "\"arguments\": object} or null, \"needs_confirmation\": bool, "
    "\"confidence\": 0..1}. No prose outside the JSON object."
)


def _contract(assistant_message: str, action, needs_confirmation: bool,
              confidence: float) -> str:
    return json.dumps({
        "assistant_message": assistant_message,
        "action": action,
        "needs_confirmation": needs_confirmation,
        "confidence": confidence,
    }, ensure_ascii=False)


# (user phrasing, assistant_message, action spec, needs_confirmation, confidence)
PIVOT = [
    # gmail
    ("Draft an email to Priya thanking her for the proposal and ask for pricing by Friday.",
     "I can draft that email and leave it ready for review.",
     {"type": "gmail.draft_email", "arguments": {"recipient_name": "Priya", "purpose": "thank her for the proposal and ask for pricing by Friday", "tone": "professional"}}, True, 0.92),
    ("Email our supplier about the delayed shipment and ask for a new delivery date.",
     "I can draft a polite follow-up to the supplier.",
     {"type": "gmail.draft_email", "arguments": {"recipient_name": "supplier", "topic": "delayed shipment", "ask": "new delivery date", "tone": "firm but polite"}}, True, 0.9),
    ("Write a short thank-you note to the new client after the kickoff call.",
     "I can draft a thank-you note for the new client.",
     {"type": "gmail.draft_email", "arguments": {"recipient_name": "new client", "purpose": "thank you after kickoff call", "tone": "warm"}}, True, 0.93),
    ("Find the email where the accountant quoted the monthly bookkeeping rate.",
     "I can search your inbox for that quote.",
     {"type": "gmail.search_email", "arguments": {"query": "bookkeeping monthly rate quote accountant"}}, False, 0.85),
    ("Search my inbox for the lease renewal notice from the landlord.",
     "I can look for the lease renewal notice.",
     {"type": "gmail.search_email", "arguments": {"query": "lease renewal notice landlord"}}, False, 0.86),
    # calendar
    ("Find a time next week for me and Jordan to discuss the Q3 budget.",
     "I can look for availability next week for a Q3 budget discussion.",
     {"type": "calendar.find_availability", "arguments": {"attendees": ["Jordan"], "date_range": "next week", "topic": "Q3 budget discussion"}}, True, 0.88),
    ("Book a 30-minute call with the web designer on Thursday afternoon.",
     "I can schedule that call for Thursday afternoon.",
     {"type": "calendar.create_event", "arguments": {"attendee": "web designer", "duration_minutes": 30, "window": "Thursday afternoon"}}, True, 0.9),
    ("Put a reminder on my calendar to pay payroll on the 28th.",
     "I can add that payroll reminder.",
     {"type": "calendar.create_event", "arguments": {"title": "Pay payroll", "date": "28th", "reminder": True}}, True, 0.94),
    ("When is everyone free for the weekly staff check-in this month?",
     "I can check everyone's availability for the staff check-in.",
     {"type": "calendar.find_availability", "arguments": {"attendees": ["staff"], "date_range": "this month", "topic": "weekly staff check-in"}}, True, 0.87),
    # docs
    ("Make a Google Doc for onboarding the new sales rep.",
     "I can create an onboarding document for the new sales rep.",
     {"type": "docs.create_document", "arguments": {"title": "Sales Rep Onboarding", "outline": ["First week goals", "Tools and accounts", "Sales process", "Key contacts"]}}, True, 0.9),
    ("Create a doc with the equipment inventory checklist we use on site.",
     "I can create an equipment inventory checklist document.",
     {"type": "docs.create_document", "arguments": {"title": "Equipment Inventory Checklist", "outline": ["Item", "Serial number", "Location", "Condition"]}}, True, 0.91),
    ("Summarize the vendor contract that's in Drive so I can skim it before the meeting.",
     "I can summarize the vendor contract for you.",
     {"type": "docs.summarize_document", "arguments": {"source": "vendor contract in Drive", "format": "bullet points, one page"}}, False, 0.84),
    # sheets
    ("Read the totals row from this month's sales sheet.",
     "I can pull the totals from the sales sheet.",
     {"type": "sheets.read_range", "arguments": {"spreadsheet": "sales", "range": "totals row this month"}}, False, 0.86),
    ("Update the expenses sheet with the $240 software subscription for August.",
     "I can add that expense line for August.",
     {"type": "sheets.update_range", "arguments": {"spreadsheet": "expenses", "entry": {"item": "software subscription", "amount": 240, "month": "August"}}}, True, 0.92),
    ("Put the new client's contact info into the CRM sheet.",
     "I can add the new client to the CRM sheet.",
     {"type": "sheets.update_range", "arguments": {"spreadsheet": "CRM", "entry": {"client": "new client", "fields": ["name", "email", "phone"]}}}, True, 0.9),
    ("Pull the unpaid invoice amounts from the accounts sheet.",
     "I can read the unpaid invoice amounts.",
     {"type": "sheets.read_range", "arguments": {"spreadsheet": "accounts", "range": "unpaid invoices"}}, False, 0.85),
    # drive
    ("Find the signed insurance policy in Drive.",
     "I can search Drive for the signed insurance policy.",
     {"type": "drive.search_files", "arguments": {"query": "signed insurance policy"}}, False, 0.87),
    ("Look for the supplier quotes folder we keep for comparisons.",
     "I can find the supplier quotes folder.",
     {"type": "drive.search_files", "arguments": {"query": "supplier quotes", "mime": "folder"}}, False, 0.85),
    # web
    ("Search the web for three competitors to our appointment booking product.",
     "I can research three competitors for your appointment booking product.",
     {"type": "web.search", "arguments": {"query": "appointment booking software competitors small business", "max_results": 3}}, False, 0.86),
    ("Find current rates for small-business liability insurance in Texas.",
     "I can look up small-business liability insurance rates for Texas.",
     {"type": "web.search", "arguments": {"query": "small business liability insurance rates Texas", "max_results": 5}}, False, 0.84),
    ("Check what a basic POS system costs for a coffee shop this year.",
     "I can research POS system pricing for coffee shops.",
     {"type": "web.search", "arguments": {"query": "POS system cost coffee shop 2026", "max_results": 5}}, False, 0.83),
    # spotify
    ("Play the focus playlist while I work on invoices.",
     "I can start a focus playlist for you.",
     {"type": "spotify.play", "arguments": {"query": "focus playlist"}}, False, 0.84),
    ("Search for a calm instrumental station for the waiting area.",
     "I can find a calm instrumental station.",
     {"type": "spotify.search", "arguments": {"query": "calm instrumental", "kind": "station"}}, False, 0.82),
    # workflow
    ("Every Monday, send the team a summary of last week's sales and remind them to log hours.",
     "I can set up that weekly workflow: sales summary + hours reminder every Monday.",
     {"type": "workflow.multi_step", "arguments": {"schedule": "every Monday", "steps": [{"action": "sheets.read_range", "arguments": {"spreadsheet": "sales", "range": "last week"}}, {"action": "gmail.draft_email", "arguments": {"recipient_name": "team", "purpose": "last week's sales summary"}}, {"action": "calendar.create_event", "arguments": {"title": "Log hours reminder", "reminder": True}}]}}, True, 0.89),
    ("When a new invoice is paid, mark it paid in the accounts sheet and email the client a receipt.",
     "I can set up that automation: mark paid + email receipt when an invoice is paid.",
     {"type": "workflow.multi_step", "arguments": {"trigger": "invoice paid", "steps": [{"action": "sheets.update_range", "arguments": {"spreadsheet": "accounts", "entry": {"status": "paid"}}}, {"action": "gmail.draft_email", "arguments": {"recipient_name": "client", "purpose": "payment receipt"}}]}}, True, 0.88),
    # no-action
    ("What can you help me automate?",
     "I can help prepare emails, calendar events, documents, sheet updates, file searches, web research, and music actions when those tools are connected.",
     None, False, 0.95),
    ("Do you have access to my inventory system?",
     "I don't have an inventory tool connected yet — I can note that as a setup item.",
     None, False, 0.9),
    ("Remind me what our store hours are.",
     "That's a fixed fact — I can store and recall it once you tell me.",
     None, False, 0.88),
]


def _chat(system: str, user: str, assistant: str) -> dict:
    return {"messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]}


def main() -> None:
    registry = build_general_registry()
    dispatch_system = system_message(registry)
    print(f"production dispatch prompt: {len(dispatch_system)} chars")

    all_rows = [json.loads(l) for l in
                (DATA / "instruction_pairs.jsonl").open(encoding="utf-8")]
    held_uuids = {json.loads(l)["uuid"] for l in
                  (DATA / "held_out.jsonl").open(encoding="utf-8")}

    train_pairs = [r for r in all_rows if r["uuid"] not in held_uuids]
    valid_pairs = [r for r in all_rows if r["uuid"] in held_uuids]
    print(f"pairs: {len(all_rows)} total -> train {len(train_pairs)} / valid {len(valid_pairs)}")

    train_rows = []
    for r in train_pairs:
        if not r.get("user", "").strip() or not r.get("assistant", "").strip():
            continue
        train_rows.append(_chat(dispatch_system, r["user"], r["assistant"]))

    pivot_rows = []
    for user, msg, action, confirm, conf in PIVOT:
        pivot_rows.append(_chat(PIVOT_SYSTEM, user,
                                _contract(msg, action, confirm, conf)))
    print(f"dispatcher rows: {len(train_rows)} | pivot action rows: {len(pivot_rows)}")

    rng = random.Random(42)
    rng.shuffle(train_rows)
    all_train = pivot_rows + train_rows
    rng.shuffle(all_train)

    with (DATA / "v2_train.jsonl").open("w", encoding="utf-8") as f:
        for row in all_train:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    valid_rows = []
    for r in valid_pairs:
        if not r.get("user", "").strip() or not r.get("assistant", "").strip():
            continue
        valid_rows.append(_chat(dispatch_system, r["user"], r["assistant"]))
    with (DATA / "v2_valid.jsonl").open("w", encoding="utf-8") as f:
        for row in valid_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"WROTE v2_train.jsonl ({len(all_train)} rows) + v2_valid.jsonl ({len(valid_rows)} rows)")


if __name__ == "__main__":
    main()
