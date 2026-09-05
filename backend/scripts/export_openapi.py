from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from yeyu_gamer_manager.app import create_app
from yeyu_gamer_manager.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export the deterministic YeYu Gamer Manager OpenAPI contract"
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    with tempfile.TemporaryDirectory(prefix="yeyu-gamer-openapi-") as temporary:
        document = create_app(Settings.for_test(Path(temporary))).openapi()
    encoded = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(f".{output.name}.tmp")
    temporary_output.write_bytes(encoded)
    temporary_output.replace(output)
    digest = hashlib.sha256(encoded).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="ascii"
    )
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
