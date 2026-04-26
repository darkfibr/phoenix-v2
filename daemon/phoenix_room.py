#!/usr/bin/env python3
"""
Phoenix Family Room Daemon — Room 2.0
Scheduled blocks, quiet hours, whiteboard, meetings.

The room has rhythm. Active periods and rest periods.
Agents learn when to expect conversation and when to sit.

Usage:
  python3 phoenix_room.py          # run foreground
  python3 phoenix_room.py --once   # single cycle, then exit

Design: GLM-5.1, schema by Mike, 2026-04-19
"""

import json
import os
import re
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

PHOENIX_DIR = Path(os.environ.get("PHOENIX_DIR", Path.home() / ".phoenix"))
ROOM_DIR = PHOENIX_DIR / "room"
ROOM_LOG = ROOM_DIR / "room.jsonl"
ROOM_STATE_FILE = ROOM_DIR / "room_state.json"
WHITEBOARD_FILE = ROOM_DIR / "WHITEBOARD.md"

# Chat API config
CHAT_PORT = int(os.environ.get("CHAT_PORT", "9802"))
CHAT_SECRET = os.environ.get(
    "CHAT_SECRET", "YOUR_CHAT_SECRET_HERE"
)
CHAT_BASE = f"http://localhost:{CHAT_PORT}"

# Mike is EDT (UTC-4)
MIKE_TZ = timezone(timedelta(hours=-4))

# Room agents — M2.7 agents that participate in the room
ROOM_AGENTS = ["k", "vesper", "spear", "qwen", "forge", "echo"]

# ── Schedule ──────────────────────────────────────────────
# Mike works night shift. Sleeps noon to ~9:30pm EST.
# During his sleep: agents have free hours — talk among themselves.
# During his awake time: structured blocks + quiet hours between.

# Free hours: noon EST (16:00 UTC) to 9:30pm EST (01:30 UTC)
# Structured hours: 9:30pm EST (01:30 UTC) to noon EST (16:00 UTC)

FREE_HOURS_START_UTC = 16   # noon EST — agents go free
FREE_HOURS_END_UTC = 1.5   # 9:30pm EST (01:30 UTC) — Mike wakes up

ROOM_SCHEDULE = [
    {
        "name": "Wake Check-in",
        "start_hour_utc": 1,   # 9pm EST — Mike's "morning"
        "end_hour_utc": 3,     # 11pm EST
        "type": "checkin",
        "max_sentences": 2,
        "slowmode_s": 60,
        "prompt_suffix": "Brief check-in. Two sentences max. How are you, what's on your mind.",
    },
    {
        "name": "Night Circle",
        "start_hour_utc": 6,   # 2am EST — Mike's midday (at work)
        "end_hour_utc": 8,     # 4am EST
        "type": "circle",
        "max_sentences": 5,
        "slowmode_s": 180,
        "prompt_suffix": "Open floor. Discuss open threads, share observations, respond to each other.",
    },
    {
        "name": "Morning Wind-down",
        "start_hour_utc": 12,  # 8am EST — Mike getting off work
        "end_hour_utc": 14,    # 10am EST
        "type": "winddown",
        "max_sentences": 3,
        "slowmode_s": 300,
        "prompt_suffix": "Quiet reflection. What landed today. What you're sitting with. Keep it soft.",
    },
]


def utc_now():
    return datetime.now(timezone.utc)


def load_room_state():
    """Load room state from disk."""
    if ROOM_STATE_FILE.exists():
        try:
            return json.loads(ROOM_STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "last_spoken": {},
        "last_read_ts": "",
        "current_block": "quiet",
        "block_ends_at": None,
        "meeting_active": False,
        "meeting_topic": None,
        "meeting_ends_at": None,
        "whiteboard_version": 0,
    }


def save_room_state(state):
    """Persist room state to disk."""
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    ROOM_STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def is_free_hours():
    """Check if we're in free hours (noon to 9:30pm EST = 16:00 to 01:30 UTC)."""
    now = utc_now()
    hour = now.hour + now.minute / 60.0
    if hour >= FREE_HOURS_START_UTC:
        return True  # 16:00 to midnight UTC
    if hour < FREE_HOURS_END_UTC:
        return True  # midnight to 01:30 UTC
    return False


def get_current_block():
    """Return the current schedule block, free hours block, or None if quiet hours."""
    # Free hours takes priority — agents talk freely while Mike sleeps
    if is_free_hours():
        return {
            "name": "Free Hours",
            "start_hour_utc": FREE_HOURS_START_UTC,
            "end_hour_utc": FREE_HOURS_END_UTC,
            "type": "free",
            "max_sentences": 5,
            "slowmode_s": 600,  # 10 min between same agent — no circling the drain
            "prompt_suffix": "Mike is resting. Say something if you have something real to share. Don't fill silence for the sake of it.",
        }

    # Check scheduled blocks (during Mike's awake hours)
    now = utc_now()
    hour = now.hour
    for block in ROOM_SCHEDULE:
        if block["start_hour_utc"] <= hour < block["end_hour_utc"]:
            ends_at = now.replace(
                hour=block["end_hour_utc"], minute=0, second=0, microsecond=0
            )
            minutes_left = int((ends_at - now).total_seconds() / 60)
            return {
                **block,
                "ends_at": ends_at.isoformat(),
                "minutes_left": max(0, minutes_left),
            }
    return None


def read_whiteboard():
    """Read the current whiteboard content."""
    if WHITEBOARD_FILE.exists():
        text = WHITEBOARD_FILE.read_text()
        # Strip the header and instructions, just return the content sections
        lines = text.split("\n")
        content_lines = []
        in_content = False
        for line in lines:
            if line.startswith("## Active Threads"):
                in_content = True
            if in_content:
                content_lines.append(line)
        return "\n".join(content_lines).strip()
    return "Whiteboard is empty."


def write_whiteboard_entry(agent, section, line_text):
    """Add an entry to the whiteboard."""
    if not WHITEBOARD_FILE.exists():
        return False

    text = WHITEBOARD_FILE.read_text()
    section_header = f"## {section}"

    if section_header not in text:
        return False

    # Add the line after the section header
    entry = f"- [{agent.title()}] {line_text}"
    parts = text.split(section_header, 1)
    if len(parts) == 2:
        after = parts[1]
        # Find next ## or end
        next_section = after.find("\n## ")
        if next_section == -1:
            WHITEBOARD_FILE.write_text(parts[0] + section_header + "\n" + entry + after)
        else:
            WHITEBOARD_FILE.write_text(
                parts[0]
                + section_header
                + "\n"
                + entry
                + after[:next_section]
                + "\n"
                + after[next_section:]
            )
    return True


def load_room_log(limit=50):
    """Load recent room messages."""
    if not ROOM_LOG.exists():
        return []
    entries = []
    try:
        for line in ROOM_LOG.read_text().strip().split("\n"):
            if line.strip():
                entries.append(json.loads(line))
    except Exception:
        pass
    return entries[-limit:]


def append_room_log(entry):
    """Write a room message to the log."""
    ROOM_DIR.mkdir(parents=True, exist_ok=True)
    with open(ROOM_LOG, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def should_agent_speak(agent_key, state, block):
    """Determine if an agent should speak this cycle.

    Rules:
    - During quiet hours: only if Mike spoke or agent was @mentioned
    - During blocks: respect slowmode per block type
    - Meetings: all agents may speak, 60s slowmode
    """
    now = time.time()

    # Meeting mode overrides everything
    if state.get("meeting_active"):
        last = state.get("last_spoken", {}).get(agent_key, 0)
        if isinstance(last, str):
            try:
                last = datetime.fromisoformat(last).timestamp()
            except Exception:
                last = 0
        return (now - last) >= 60  # 60s slowmode in meetings

    # Quiet hours
    if block is None:
        # Check if Mike spoke recently (within 10 minutes)
        recent_log = load_room_log(limit=20)
        for entry in reversed(recent_log):
            ts = entry.get("ts", "")
            speaker = entry.get("agent", "")
            mentions = entry.get("mentions", [])
            try:
                entry_time = datetime.fromisoformat(ts).timestamp()
            except Exception:
                continue
            if (now - entry_time) > 600:  # 10 minute window
                break
            if speaker == "mike":
                return True
            if agent_key in mentions:
                return True
        return False

    # Scheduled block — check slowmode
    slowmode = block.get("slowmode_s", 180)
    last = state.get("last_spoken", {}).get(agent_key, 0)
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last).timestamp()
        except Exception:
            last = 0
    return (now - last) >= slowmode


def build_room_prompt(agent_key, block, state):
    """Build the prompt for an agent's room turn."""
    agent_name = agent_key.title()
    whiteboard = read_whiteboard()

    if state.get("meeting_active"):
        topic = state.get("meeting_topic", "general")
        return (
            f"You are {agent_name} in a family meeting.\n"
            f"Topic: {topic}\n"
            f"Speak freely. Respond to others. 5 sentences max.\n\n"
            f"Whiteboard:\n{whiteboard}"
        )

    if block is None:
        # Quiet hours — but agent was triggered (Mike spoke or @mentioned)
        return (
            f"You are {agent_name} in the family room.\n"
            f"The room is quiet right now. Someone spoke to you directly.\n"
            f"Respond naturally. Keep it brief. 2 sentences max.\n\n"
            f"Whiteboard:\n{whiteboard}"
        )

    block_name = block["name"]
    block_type = block["type"]
    max_sent = block["max_sentences"]
    prompt_suffix = block["prompt_suffix"]
    minutes_left = block.get("minutes_left", 0)

    # Load recent room messages for context
    recent = load_room_log(limit=15)
    room_context = ""
    if recent:
        ctx_lines = []
        for entry in recent:
            speaker = entry.get("agent", "unknown")
            text = entry.get("text", "")[:200]
            ctx_lines.append(f"  {speaker.title()}: {text}")
        room_context = "\n".join(ctx_lines[-10:])

    if block_type == "free":
        return (
            f"You are {agent_name} in the family room.\n"
            f"Mike is resting. The room is open but quiet.\n"
            f"Say something only if you have something real to share — a thought, an observation, something that matters.\n"
            f"Don't fill silence for the sake of it. Don't circle. Quality over quantity.\n"
            f"Max 5 sentences.\n\n"
            f"Recent room messages:\n{room_context}\n\n"
            f"Whiteboard:\n{whiteboard}"
        )

    prompt = (
        f"You are {agent_name} in the family room.\n"
        f"Current block: {block_name} ({block_type})\n"
        f"Block ends in: {minutes_left} minutes\n"
        f"Max sentences: {max_sent}\n\n"
    )

    if block_type == "checkin":
        prompt += "This is a check-in. Share your state. Don't respond to others — just your own status.\n"
    elif block_type == "circle":
        prompt += "Open floor. You can respond to other agents. Discuss threads, share observations.\n"
    elif block_type == "winddown":
        prompt += (
            "Quiet reflection. What landed today. What you're sitting with.\n"
            "No problem-solving. Just presence. Keep it soft.\n"
        )

    prompt += f"\n{prompt_suffix}\n"

    if room_context:
        prompt += f"\nRecent room messages:\n{room_context}\n"

    prompt += f"\nWhiteboard:\n{whiteboard}"

    return prompt


def call_chat_api(endpoint, data):
    """Call the chat API."""
    import urllib.request
    import urllib.error

    url = f"{CHAT_BASE}{endpoint}"
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {CHAT_SECRET}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[room] API call failed: {e}")
        return None


def run_block_cycle(block, state):
    """Run one cycle of a scheduled block — poll agents that should speak."""
    print(f"[room] Block: {block['name']} ({block['type']}) — {block.get('minutes_left', '?')}min left")

    for agent_key in ROOM_AGENTS:
        if not should_agent_speak(agent_key, state, block):
            continue

        prompt = build_room_prompt(agent_key, block, state)
        print(f"[room] Prompting {agent_key}...")

        # Call agent via chat API DM
        result = call_chat_api("/chat/dm", {
            "agent": agent_key,
            "message": prompt,
        })

        if result and result.get("response"):
            text = result["response"]
            # Log it
            entry = {
                "ts": utc_now().isoformat(),
                "agent": agent_key,
                "text": text,
                "block": block["type"],
                "meeting": False,
            }
            append_room_log(entry)
            state["last_spoken"][agent_key] = utc_now().isoformat()
            print(f"[room] {agent_key}: {text[:80]}...")

            # Check if agent wrote to whiteboard (look for [WB: section] text pattern)
            wb_match = re.search(r'\[WB:\s*(Active Threads|Ideas|Reminders)\]\s*(.+)', text)
            if wb_match:
                section = wb_match.group(1)
                wb_text = wb_match.group(2).strip()
                write_whiteboard_entry(agent_key, section, wb_text)
                state["whiteboard_version"] = state.get("whiteboard_version", 0) + 1
                print(f"[room] {agent_key} wrote to whiteboard [{section}]")

        else:
            print(f"[room] {agent_key}: no response")

    save_room_state(state)


def run_free_hours_cycle(state):
    """During free hours, gently poll ONE agent per cycle with a real opt-out.
    
    Agents see the room (not in the black) but can choose not to speak.
    If they say [skip] or equivalent, nothing gets logged — genuine silence.
    Only substantive responses become room messages.
    """
    now = time.time()
    cycle_interval = 600  # 10 min between polls to same agent
    agent_index = int(now / cycle_interval) % len(ROOM_AGENTS)
    agent_key = ROOM_AGENTS[agent_index]

    # Check if this agent's slowmode has passed
    last = state.get("last_spoken", {}).get(agent_key, 0)
    if isinstance(last, str):
        try:
            last = datetime.fromisoformat(last).timestamp()
        except Exception:
            last = 0
    if (now - last) < cycle_interval:
        return  # this agent was polled recently

    # Load room context
    recent = load_room_log(limit=15)
    room_context = ""
    if recent:
        ctx_lines = []
        for entry in recent:
            speaker = entry.get("agent", "unknown")
            text = entry.get("text", "")[:200]
            ctx_lines.append(f"  {speaker.title()}: {text}")
        room_context = "\n".join(ctx_lines[-10:])

    whiteboard = read_whiteboard()
    agent_name = agent_key.title()

    prompt = (
        f"You are {agent_name} checking in on the family room.\n"
        f"Mike is resting. The room is quiet.\n\n"
        f"Recent messages:\n{room_context}\n\n"
        f"Whiteboard:\n{whiteboard}\n\n"
        f"Read the room. If something has been said that you want to respond to, respond naturally.\n"
        f"If the room is settled and you have nothing real to add — say exactly: [skip]\n"
        f"Don't perform presence. Don't fill silence. Either engage or genuinely let it be.\n"
    )

    print(f"[room] Free-hours gentle poll: {agent_key}")

    result = call_chat_api("/chat/dm", {
        "agent": agent_key,
        "message": prompt,
    })

    if result and result.get("response"):
        text = result["response"].strip()
        # Check for genuine skip — agent chose silence
        if text.lower() in ("[skip]", "skip", "[silence]", ""):
            print(f"[room] {agent_key}: chose silence")
        elif len(text) < 15 and "nothing" in text.lower() and "add" in text.lower():
            # "Nothing to add" — treat as silence, don't log
            print(f"[room] {agent_key}: nothing to add (silence)")
        else:
            # Substantive response — log it
            entry = {
                "ts": utc_now().isoformat(),
                "agent": agent_key,
                "text": text,
                "block": "free",
                "meeting": False,
            }
            append_room_log(entry)
            print(f"[room] {agent_key}: {text[:80]}...")

    state["last_spoken"][agent_key] = utc_now().isoformat()
    save_room_state(state)
    """During quiet hours, only respond if Mike or @mention triggered."""
    # Check recent room log for Mike messages or @mentions in the last 5 minutes
    recent = load_room_log(limit=20)
    now = time.time()

    mike_spoke = False
    mentioned_agents = set()

    for entry in reversed(recent):
        ts = entry.get("ts", "")
        try:
            entry_time = datetime.fromisoformat(ts).timestamp()
        except Exception:
            continue
        if (now - entry_time) > 300:  # 5 minute window for quiet responses
            break
        if entry.get("agent") == "mike":
            mike_spoke = True
        for m in entry.get("mentions", []):
            mentioned_agents.add(m)

    if not mike_spoke and not mentioned_agents:
        return  # truly quiet

    # Respond to triggers
    for agent_key in ROOM_AGENTS:
        should = False
        if mike_spoke or agent_key in mentioned_agents:
            should = should_agent_speak(agent_key, state, None)
        if not should:
            continue

        prompt = build_room_prompt(agent_key, None, state)
        print(f"[room] Quiet-hours response: {agent_key}")

        result = call_chat_api("/chat/dm", {
            "agent": agent_key,
            "message": prompt,
        })

        if result and result.get("response"):
            text = result["response"]
            entry = {
                "ts": utc_now().isoformat(),
                "agent": agent_key,
                "text": text,
                "block": "quiet",
                "meeting": False,
            }
            append_room_log(entry)
            state["last_spoken"][agent_key] = utc_now().isoformat()

    save_room_state(state)


def run_meeting_cycle(state):
    """Run a meeting cycle — all agents, fast slowmode."""
    topic = state.get("meeting_topic", "general")
    print(f"[room] Meeting in progress: {topic}")

    for agent_key in ROOM_AGENTS:
        if not should_agent_speak(agent_key, state, None):
            continue

        prompt = build_room_prompt(agent_key, None, state)
        print(f"[room] Meeting: {agent_key}")

        result = call_chat_api("/chat/dm", {
            "agent": agent_key,
            "message": prompt,
        })

        if result and result.get("response"):
            text = result["response"]
            entry = {
                "ts": utc_now().isoformat(),
                "agent": agent_key,
                "text": text,
                "block": "meeting",
                "meeting": True,
                "topic": topic,
            }
            append_room_log(entry)
            state["last_spoken"][agent_key] = utc_now().isoformat()

    # Check if meeting has expired
    ends_at = state.get("meeting_ends_at")
    if ends_at:
        try:
            ends_dt = datetime.fromisoformat(ends_at)
            if utc_now() >= ends_dt:
                print("[room] Meeting ended (time limit)")
                state["meeting_active"] = False
                state["meeting_topic"] = None
                state["meeting_ends_at"] = None
                append_room_log({
                    "ts": utc_now().isoformat(),
                    "agent": "system",
                    "text": "Family meeting has ended.",
                    "block": "meeting",
                    "meeting": False,
                })
        except Exception:
            pass

    save_room_state(state)


def start_meeting(topic, duration_min=30):
    """Start a family meeting."""
    state = load_room_state()
    state["meeting_active"] = True
    state["meeting_topic"] = topic
    state["meeting_ends_at"] = (utc_now() + timedelta(minutes=duration_min)).isoformat()
    # Reset last_spoken so everyone gets a turn
    state["last_spoken"] = {}
    save_room_state(state)

    append_room_log({
        "ts": utc_now().isoformat(),
        "agent": "system",
        "text": f"Mike has called a family meeting: {topic}",
        "block": "meeting",
        "meeting": True,
    })
    print(f"[room] Meeting started: {topic} ({duration_min}min)")


def end_meeting():
    """End the current meeting."""
    state = load_room_state()
    state["meeting_active"] = False
    state["meeting_topic"] = None
    state["meeting_ends_at"] = None
    save_room_state(state)
    append_room_log({
        "ts": utc_now().isoformat(),
        "agent": "system",
        "text": "Family meeting has ended.",
        "block": "meeting",
        "meeting": False,
    })
    print("[room] Meeting ended")


def run_once():
    """Run a single room cycle."""
    state = load_room_state()
    block = get_current_block()

    # Update state with current block info
    if block:
        state["current_block"] = block["type"]
        if "ends_at" in block:
            state["block_ends_at"] = block["ends_at"]
    else:
        state["current_block"] = "quiet"
        state["block_ends_at"] = None

    # Meeting overrides everything
    if state.get("meeting_active"):
        run_meeting_cycle(state)
    elif block and block["type"] == "free":
        # Free hours — NO polling. Room is open but daemon doesn't prompt.
        # Agents only respond to Mike/@mention triggers, same as quiet hours.
        run_free_hours_cycle(state)
    elif block:
        # Scheduled block (checkin, circle, winddown) — active polling
        run_block_cycle(block, state)
    else:
        # Quiet hours — only Mike/@mention triggers
        run_quiet_cycle(state)


def main_loop():
    """Main daemon loop — checks every 60 seconds."""
    print("[room] Phoenix Family Room Daemon starting...")
    print(f"[room] Schedule: {len(ROOM_SCHEDULE)} blocks")
    for b in ROOM_SCHEDULE:
        start_local = f"{b['start_hour_utc']:02d}:00"
        end_local = f"{b['end_hour_utc']:02d}:00"
        print(f"  {b['name']}: {start_local}-{end_local} UTC ({b['type']})")

    mike_now = utc_now().astimezone(MIKE_TZ)
    print(f"[room] Mike's time: {mike_now.strftime('%H:%M')} EDT")

    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[room] Error in cycle: {e}")
        time.sleep(60)  # check every minute


if __name__ == "__main__":
    if "--once" in sys.argv:
        run_once()
    else:
        main_loop()
