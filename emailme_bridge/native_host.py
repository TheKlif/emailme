import json
import struct
import sys
from datetime import datetime
from pathlib import Path

# config.py lives one folder up (emailme/), not alongside this script
# (emailme/emailme_bridge/) - path must be extended before importing it,
# so this import can't be moved up with the rest or reordered by isort.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import VAULT_ROOT  # isort: skip

VAULT_PATH = Path(VAULT_ROOT)


def read_message():
    """Reads one native-messaging message from stdin: a 4-byte length prefix, then that many bytes of JSON."""
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        sys.exit(0)
    message_length = struct.unpack("=I", raw_length)[0]
    message = sys.stdin.buffer.read(message_length).decode("utf-8")
    return json.loads(message)


def send_message(message_content):
    """Writes one native-messaging response to stdout using the same length-prefixed framing."""
    encoded_content = json.dumps(message_content).encode("utf-8")
    encoded_length = struct.pack("=I", len(encoded_content))
    sys.stdout.buffer.write(encoded_length)
    sys.stdout.buffer.write(encoded_content)
    sys.stdout.buffer.flush()


def unique_capture_path(vault_path: Path, timestamp: str) -> Path:
    """
    Returns vault_path/capture_<timestamp>.md, or _2/_3/etc if that name is
    already taken. Guards against two captures in the same second silently
    overwriting each other.
    """
    candidate = vault_path / f"capture_{timestamp}.md"
    counter = 2
    while candidate.exists():
        candidate = vault_path / f"capture_{timestamp}_{counter}.md"
        counter += 1
    return candidate


def main():
    """Reads one capture payload from the browser extension and writes it as a new vault note."""
    message = read_message()
    timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    output_file = unique_capture_path(VAULT_PATH, timestamp)

    parts = []
    if message.get("selection"):
        parts.append(message["selection"])
    if message.get("linkUrl"):
        parts.append(message["linkUrl"])
    if message.get("srcUrl"):
        parts.append(message["srcUrl"])
    if not message.get("linkUrl") and not message.get("srcUrl") and message.get("url"):
        parts.append(message["url"])

    content = "\n".join(parts) if parts else message.get("title", "")

    output_file.write_text(content, encoding="utf-8")
    send_message({"status": "ok", "wrote": str(output_file)})


if __name__ == "__main__":
    main()
