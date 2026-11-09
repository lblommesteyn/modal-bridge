"""Export the real API contract without credentials or Modal calls."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from modal_bridge.app import create_app

with TemporaryDirectory() as directory:
    app = create_app(token="schema-generation-placeholder-token", targets={"square": ("demo", "square")},
                     db_path=f"{directory}/jobs.sqlite")
    path = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    path.write_text(json.dumps(app.openapi(), indent=2) + "\n")
    print(path)
