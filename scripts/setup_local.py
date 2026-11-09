"""Create a private, ignored .env for the real gateway, without printing its token."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
path = root / ".env"
content = (f"BRIDGE_TOKEN={secrets.token_urlsafe(32)}\n"
           'BRIDGE_TARGETS={"square":["modal-bridge-example","square"],"fail":["modal-bridge-example","fail"],"slow":["modal-bridge-example","slow"]}\n'
           f"BRIDGE_DB={root / 'data/jobs.sqlite'}\n")
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print(f"Keeping existing configuration: {path}")
else:
    with os.fdopen(fd, "w") as file:
        file.write(content)
    print(f"Created private configuration: {path}")
print("Modal credentials use your active Modal profile or MODAL_TOKEN_ID / MODAL_TOKEN_SECRET.")
