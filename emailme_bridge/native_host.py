import sys
import json
import struct
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import VAULT_ROOT
VAULT_PATH = Path(VAULT_ROOT)

def read_message():
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        sys.exit(0)
    message_length = struct.unpack('=I', raw_length)[0]
    message = sys.stdin.buffer.read(message_length).decode('utf-8')
    return json.loads(message)

def send_message(message_content):
    encoded_content = json.dumps(message_content).encode('utf-8')
    encoded_length = struct.pack('=I', len(encoded_content))
    sys.stdout.buffer.write(encoded_length)
    sys.stdout.buffer.write(encoded_content)
    sys.stdout.buffer.flush()

def main():
    message = read_message()
    timestamp = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    output_file = VAULT_PATH / f"capture_{timestamp}.md"

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