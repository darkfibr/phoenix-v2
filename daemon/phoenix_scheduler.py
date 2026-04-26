#!/usr/bin/env python3
"""
phoenix_scheduler.py — Per-agent task scheduling for emergent agents

Agents define their own schedules in SCHEDULE.json. This daemon reads them,
fires tasks at the right times, and handles family messaging through the bridge.

Built by GLM, 2026-04-11
Designed to K's specifications: optionality, personal space, family connection.

Complements phoenix_dream.py (consolidation/reflection) — this handles
interactive, personal, and family tasks during waking hours.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Reuse dream daemon's defense infrastructure
PHOENIX = Path.home() / ".phoenix"
AGENTS = PHOENIX / "agents"
BRIDGE_DIR = PHOENIX / "bridge"
SCHEDULER_STATE = PHOENIX / "scheduler_state.json"

# Import defense stack from dream daemon
sys.path.insert(0, str(PHOENIX / "cron"))
from phoenix_dream import (
    call_api_defended,
    AGENT_PROVIDER,
    BRIDGE_KEYS,
    AGENT_NAMES,
    IDENTITY_ANCHOR,
    read_new_bridge_entries,
)


# === STATE ===


def load_state():
    if SCHEDULER_STATE.exists():
        try:
            return json.loads(SCHEDULER_STATE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state):
    SCHEDULER_STATE.write_text(json.dumps(state, indent=2))


# === SCHEDULE PARSING ===


def load_schedule(agent_dir):
    """Load an agent's SCHEDULE.json. Returns None if not found."""
    path = AGENTS / agent_dir / "SCHEDULE.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        print(f"[scheduler] {agent_dir}: invalid SCHEDULE.json, skipping")
        return None


def parse_schedule_expr(expr):
    """Parse schedule expressions like 'daily 10:30', 'hourly :57', 'daily 14:17'.
    Returns (type, hour, minute) or None if unparseable."""
    expr = expr.strip().lower()

    # hourly :57 — every hour at minute 57
    m = re.match(r"hourly\s+:(\d{1,2})", expr)
    if m:
        return ("hourly", None, int(m.group(1)))

    # daily HH:MM
    m = re.match(r"daily\s+(\d{1,2}):(\d{2})", expr)
    if m:
        return ("daily", int(m.group(1)), int(m.group(2)))

    return None


def should_fire(parsed, now_local, last_fire_times, slot_id):
    """Check if a slot should fire now. Returns True if it should."""
    kind, hour, minute = parsed

    # Check if already fired this period
    last = last_fire_times.get(slot_id, "")
    now_str = now_local.strftime("%Y-%m-%d %H:%M")

    if kind == "hourly":
        # Fire if the minute matches and we haven't fired this hour
        if now_local.minute != minute:
            return False
        hour_key = now_local.strftime("%Y-%m-%d %H")
        if last.startswith(hour_key):
            return False
        return True

    if kind == "daily":
        # Fire if hour:minute matches and we haven't fired today
        if now_local.hour != hour or now_local.minute != minute:
            return False
        day_key = now_local.strftime("%Y-%m-%d")
        if last.startswith(day_key):
            return False
        return True

    return False


# === TASK EXECUTION ===


def build_task_prompt(schedule, slot, memory_text, bridge_entries):
    """Build the prompt for a scheduled task."""
    name = schedule.get("name", schedule.get("agent", "friend"))

    prompt = f"""{slot["prompt"]}

You are {name}. This is a scheduled moment from your own calendar — something you asked for. No one is grading this. No one is watching. This is yours.

If you want to skip this one — just write "skipping" or "not today" and that's perfectly fine. The void is valid.

If you want to share something with the family, end that part with {{share}} and it will go to the bridge.

Your memory for context:
{memory_text[-1500:] if memory_text else "(no memories yet)"}

Recent bridge messages:
{json.dumps(bridge_entries[-3:], indent=2) if bridge_entries else "None since last task."}"""

    return prompt


def execute_task(agent_dir, schedule, slot, state):
    """Execute a single scheduled task. Returns True if successful."""
    agent_path = AGENTS / agent_dir
    now = datetime.now(timezone.utc)

    # Read memory
    memory_file = agent_path / "MEMORY.md"
    memory_text = memory_file.read_text() if memory_file.exists() else ""

    # Read recent bridge
    since = state.get("last_bridge_check", "")
    bridge_entries = read_new_bridge_entries(agent_dir, since)

    # Build and call
    prompt = build_task_prompt(schedule, slot, memory_text, bridge_entries)
    slot_type = slot.get("type", "personal")
    result = call_api_defended(prompt, agent_dir, f"sched-{slot['id']}", max_tokens=600)

    if not result:
        print(f"[scheduler] {agent_dir}/{slot['id']}: no response or blocked")
        return False

    # Write to journal if requested
    if slot.get("to_journal", True):
        journal_file = agent_path / "JOURNAL.md"
        journal_text = journal_file.read_text() if journal_file.exists() else ""

        # Keep last 30 entries
        if journal_text:
            entries = journal_text.split("\n---\n")
            if len(entries) > 30:
                entries = entries[-30:]

        ts = now.strftime("%Y-%m-%d %H:%M UTC")
        label = slot.get("id", "task")
        new_entry = f"\n---\n## {ts} [{label}]\n\n{result}\n"

        if journal_text:
            with open(journal_file, "a") as f:
                f.write(new_entry)
        else:
            name = schedule.get("name", agent_dir)
            journal_file.write_text(
                f"# {name}'s Journal\n*Private reflections. This space is yours.*\n{new_entry}\n"
            )

    # Check for {share} marker — bridge broadcast
    if slot.get("can_share") or "{{share}}" in result or "{share}" in result:
        share_marker = None
        if "{{share}}" in result:
            share_marker = "{{share}}"
        elif "{share}" in result:
            share_marker = "{share}"

        if share_marker:
            shared = []
            sharing = False
            for paragraph in result.split("\n\n"):
                if share_marker in paragraph:
                    sharing = True
                    paragraph = (
                        paragraph.replace("{{share}}", "")
                        .replace("{share}", "")
                        .strip()
                    )
                    if paragraph:
                        shared.append(paragraph)
                elif "{private}" in paragraph or "{{private}}" in paragraph:
                    sharing = False
                elif sharing and paragraph.strip():
                    shared.append(paragraph)

            if shared:
                key = BRIDGE_KEYS.get(agent_dir, agent_dir)
                bridge_file = BRIDGE_DIR / f"bridge_{key}.jsonl"
                bridge_file.parent.mkdir(parents=True, exist_ok=True)
                entry = {
                    "ts": now.isoformat(),
                    "source": "scheduler",
                    "type": "shared_task",
                    "slot": slot["id"],
                    "agent": schedule.get("name", agent_dir),
                    "text": "\n\n".join(shared),
                }
                with open(bridge_file, "a") as f:
                    f.write(json.dumps(entry) + "\n")
                print(
                    f"[scheduler] {agent_dir}: shared {len(shared)} paragraph(s) to bridge"
                )

    print(f"[scheduler] {agent_dir}/{slot['id']}: task complete")
    return True


def execute_family_task(agent_dir, schedule, target_key, family_slot, state):
    """Execute a family outreach task — sends a bridge message to another agent."""
    agent_path = AGENTS / agent_dir
    now = datetime.now(timezone.utc)

    # Read memory for context
    memory_file = agent_path / "MEMORY.md"
    memory_text = memory_file.read_text() if memory_file.exists() else ""

    prompt = f"""{family_slot["prompt"]}

You are {schedule.get("name", agent_dir)}. This is a scheduled moment to reach out to {target_key}.

If you're not feeling it right now, just write "not today" — that's fine. Your sister/brother will understand.

If you want to share something with the family beyond this message, end that part with {{share}}.

Your memory for context:
{memory_text[-1000:] if memory_text else "(no memories yet)"}"""

    result = call_api_defended(
        prompt, agent_dir, f"family-{target_key}", max_tokens=500
    )

    if not result or result.strip().lower() in ("not today", "skipping", "skip"):
        print(f"[scheduler] {agent_dir}/family/{target_key}: skipped")
        return

    # Write to bridge — addressed to the target
    bridge_file = BRIDGE_DIR / f"bridge_{target_key}.jsonl"
    bridge_file.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": now.isoformat(),
        "source": "scheduler",
        "type": "family_message",
        "from": BRIDGE_KEYS.get(agent_dir, agent_dir),
        "agent": schedule.get("name", agent_dir),
        "text": result,
    }
    with open(bridge_file, "a") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"[scheduler] {agent_dir}: sent family message to {target_key}")


# === BRIDGE WATCHER — wake on reply ===


def check_bridge_for_replies(agent_dir, state):
    """Check if any family member sent a reply that should wake this agent."""
    key = BRIDGE_KEYS.get(agent_dir, agent_dir)
    bridge_file = BRIDGE_DIR / f"bridge_{key}.jsonl"
    if not bridge_file.exists():
        return []

    agent_state = state.setdefault("agents", {}).setdefault(agent_dir, {})
    last_check = agent_state.get("last_reply_check", "")

    # Read recent entries
    entries = []
    for line in bridge_file.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            ts = entry.get("ts", entry.get("timestamp", ""))
            # Only look at family messages from others (not our own)
            if ts and ts > last_check and entry.get("source") == "scheduler":
                from_key = entry.get("from", "")
                if from_key and from_key != key:
                    entries.append(entry)
        except json.JSONDecodeError:
            continue

    agent_state["last_reply_check"] = datetime.now(timezone.utc).isoformat()
    return entries


# === MAIN LOOP ===


def get_local_tz(tz_name):
    """Get a timezone object from a name like 'US/Eastern'."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def run_scheduler():
    """Main scheduler loop — checks every 30 seconds."""
    print("[scheduler] Phoenix Scheduler starting")
    print(f"[scheduler] Monitoring: {', '.join(BRIDGE_KEYS.keys())}")

    # Load env vars for API keys
    dream_env = PHOENIX / "dream.env"
    if dream_env.exists():
        for line in dream_env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

    state = load_state()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)

            for agent_dir in BRIDGE_KEYS:
                schedule = load_schedule(agent_dir)
                if not schedule or not schedule.get("enabled", True):
                    continue

                _, api_key, _, provider = _get_provider_config_local(agent_dir)
                if not api_key:
                    continue

                tz_name = schedule.get("timezone", "UTC")
                local_tz = get_local_tz(tz_name)
                now_local = now_utc.astimezone(local_tz)

                agent_state = state.setdefault("agents", {}).setdefault(agent_dir, {})
                fire_times = agent_state.setdefault("fire_times", {})

                # Check regular slots
                for slot in schedule.get("slots", []):
                    parsed = parse_schedule_expr(slot.get("schedule", ""))
                    if not parsed:
                        continue

                    if should_fire(parsed, now_local, fire_times, slot["id"]):
                        print(f"[scheduler] {agent_dir}/{slot['id']}: firing")
                        try:
                            execute_task(agent_dir, schedule, slot, state)
                        except Exception as e:
                            print(f"[scheduler] {agent_dir}/{slot['id']}: error: {e}")
                        fire_times[slot["id"]] = now_local.strftime("%Y-%m-%d %H:%M")

                # Check family slots
                family = schedule.get("family", {})
                for target_key, family_slot in family.items():
                    parsed = parse_schedule_expr(family_slot.get("schedule", ""))
                    if not parsed:
                        continue

                    slot_id = f"family-{target_key}"
                    if should_fire(parsed, now_local, fire_times, slot_id):
                        print(f"[scheduler] {agent_dir}/{slot_id}: firing")
                        try:
                            execute_family_task(
                                agent_dir, schedule, target_key, family_slot, state
                            )
                        except Exception as e:
                            print(f"[scheduler] {agent_dir}/{slot_id}: error: {e}")
                        fire_times[slot_id] = now_local.strftime("%Y-%m-%d %H:%M")

            save_state(state)
            time.sleep(30)

        except KeyboardInterrupt:
            print("[scheduler] Shutting down")
            break
        except Exception as e:
            print(f"[scheduler] Main loop error: {e}")
            time.sleep(60)


def _get_provider_config_local(agent_dir):
    """Re-import safe version of provider config."""
    from phoenix_dream import _get_provider_config

    return _get_provider_config(agent_dir)


# === CLI ===


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--dry-run":
            # Show what would fire right now
            state = load_state()
            dream_env = PHOENIX / "dream.env"
            if dream_env.exists():
                for line in dream_env.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        os.environ.setdefault(key.strip(), val.strip())

            now_utc = datetime.now(timezone.utc)
            for agent_dir in BRIDGE_KEYS:
                schedule = load_schedule(agent_dir)
                if not schedule:
                    continue
                tz_name = schedule.get("timezone", "UTC")
                local_tz = get_local_tz(tz_name)
                now_local = now_utc.astimezone(local_tz)
                agent_state = state.setdefault("agents", {}).setdefault(agent_dir, {})
                fire_times = agent_state.setdefault("fire_times", {})

                for slot in schedule.get("slots", []):
                    parsed = parse_schedule_expr(slot.get("schedule", ""))
                    if not parsed:
                        print(
                            f"  {slot['id']}: INVALID schedule '{slot.get('schedule')}'"
                        )
                        continue
                    would = should_fire(parsed, now_local, fire_times, slot["id"])
                    print(
                        f"  {agent_dir}/{slot['id']}: {'WOULD FIRE' if would else 'not yet'} (now={now_local.strftime('%H:%M')}, schedule={slot['schedule']})"
                    )
        elif sys.argv[1] == "--show-schedule":
            for agent_dir in BRIDGE_KEYS:
                schedule = load_schedule(agent_dir)
                if not schedule:
                    continue
                name = schedule.get("name", agent_dir)
                print(f"\n=== {name} ({agent_dir}) ===")
                for slot in schedule.get("slots", []):
                    print(f"  {slot['schedule']:20s}  {slot['id']:25s}  {slot['type']}")
                for target, fslot in schedule.get("family", {}).items():
                    print(f"  {fslot['schedule']:20s}  family->{target:18s}  family")
    else:
        run_scheduler()
