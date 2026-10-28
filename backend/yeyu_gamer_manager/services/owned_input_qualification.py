from functools import lru_cache
import json
from pathlib import Path
import re


@lru_cache(maxsize=1)
def _qualifications():
    path = Path(__file__).parent / "resources" / "owned_input_qualifications.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or set(document) != {"schemaVersion", "qualifications"}
            or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1
            or not isinstance(document["qualifications"], list)):
        raise ValueError("Invalid owned input qualification policy")
    qualified = set()
    for entry in document["qualifications"]:
        if (not isinstance(entry, dict) or set(entry) != {"gameId", "packageDigest", "hostSha256"}
                or not isinstance(entry["gameId"], str) or not entry["gameId"]
                or any(not isinstance(entry[key], str)
                       or not re.fullmatch(r"sha256:[0-9a-f]{64}", entry[key])
                       or entry[key] == "sha256:" + "0" * 64
                       for key in ("packageDigest", "hostSha256"))):
            raise ValueError("Invalid owned input qualification entry")
        identity = (entry["gameId"], entry["packageDigest"], entry["hostSha256"])
        if identity in qualified:
            raise ValueError("Duplicate owned input qualification")
        qualified.add(identity)
    return frozenset(qualified)


def owned_input_qualified(game_id, package_digest, host_sha256):
    return (game_id, package_digest, host_sha256) in _qualifications()
