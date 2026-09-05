from __future__ import annotations

import hashlib
import base64
import binascii
import os
import struct
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from yeyu_gamer_manager.services.artifact_integrity import (
    verify_artifact_entity,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ArtifactIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="yeyu-completion-integrity-"
        )
        self.root = Path(self.temporary.name) / "artifacts"
        self.root.mkdir()
        self.path = self.root / "proof.png"
        self.path.write_bytes(PNG)
        self.document = {
            "relativePath": self.path.name,
            "contentType": "image/png",
            "sizeBytes": len(PNG),
            "hash": hashlib.sha256(PNG).hexdigest(),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_entity_is_reopened_and_hashed(self) -> None:
        result = verify_artifact_entity(
            self.root, "opaque-artifact", self.document
        )

        self.assertTrue(result.valid)
        self.assertEqual(result.reason_code, "verified")
        self.assertEqual(result.content, PNG)
        self.assertEqual(result.content_hash, self.document["hash"])

    def test_short_reads_continue_on_the_same_descriptor(self) -> None:
        real_read = os.read
        requested: list[int] = []

        def short_read(descriptor: int, count: int) -> bytes:
            requested.append(count)
            return real_read(descriptor, min(count, 3))

        with mock.patch(
            "yeyu_gamer_manager.services.artifact_integrity.os.read",
            side_effect=short_read,
        ):
            result = verify_artifact_entity(
                self.root, "opaque-artifact", self.document
            )

        self.assertTrue(result.valid)
        self.assertGreater(len(requested), 1)
        self.assertTrue(all(value <= len(PNG) + 1 for value in requested))

    def test_json_entity_is_strictly_decoded_and_parsed(self) -> None:
        content = b'{"status":"diagnostic"}'
        path = self.root / "diagnostic.json"
        path.write_bytes(content)
        document = {
            "relativePath": path.name,
            "contentType": "application/json",
            "sizeBytes": len(content),
            "hash": hashlib.sha256(content).hexdigest(),
        }

        valid = verify_artifact_entity(self.root, "json-valid", document)
        self.assertTrue(valid.valid)

        malformed = b'{"status":}'
        path.write_bytes(malformed)
        malformed_document = {
            **document,
            "sizeBytes": len(malformed),
            "hash": hashlib.sha256(malformed).hexdigest(),
        }
        invalid = verify_artifact_entity(
            self.root, "json-invalid", malformed_document
        )
        self.assertFalse(invalid.valid)
        self.assertEqual(invalid.reason_code, "artifact_json_mismatch")

        non_standard = b'{"score":NaN}'
        path.write_bytes(non_standard)
        non_standard_document = {
            **document,
            "sizeBytes": len(non_standard),
            "hash": hashlib.sha256(non_standard).hexdigest(),
        }
        invalid_constant = verify_artifact_entity(
            self.root, "json-non-standard", non_standard_document
        )
        self.assertFalse(invalid_constant.valid)
        self.assertEqual(
            invalid_constant.reason_code,
            "artifact_json_mismatch",
        )

        invalid_utf8 = b'{"status":"\xff"}'
        path.write_bytes(invalid_utf8)
        invalid_utf8_document = {
            **document,
            "sizeBytes": len(invalid_utf8),
            "hash": hashlib.sha256(invalid_utf8).hexdigest(),
        }
        invalid_encoding = verify_artifact_entity(
            self.root, "json-invalid-utf8", invalid_utf8_document
        )
        self.assertFalse(invalid_encoding.valid)
        self.assertEqual(
            invalid_encoding.reason_code,
            "artifact_encoding_mismatch",
        )

    def test_missing_entity_fails_closed(self) -> None:
        self.path.unlink()

        result = verify_artifact_entity(
            self.root, "opaque-artifact", self.document
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason_code, "artifact_file_unavailable")

    def test_changed_bytes_fail_hash_revalidation(self) -> None:
        changed = b"\x89PNG\r\n\x1a\ntampered-fixture"
        self.path.write_bytes(changed)
        document = {
            **self.document,
            "sizeBytes": len(changed),
        }

        result = verify_artifact_entity(
            self.root, "opaque-artifact", document
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason_code, "artifact_hash_mismatch")
        self.assertEqual(result.content_hash, hashlib.sha256(changed).hexdigest())

    def test_truncated_and_signature_only_images_fail_full_decode(self) -> None:
        for index, content in enumerate((PNG[:-8], b"\x89PNG\r\n\x1a\nnot-an-image")):
            path = self.root / f"corrupt-{index}.png"
            path.write_bytes(content)
            document = {
                "relativePath": path.name,
                "contentType": "image/png",
                "sizeBytes": len(content),
                "hash": hashlib.sha256(content).hexdigest(),
            }
            result = verify_artifact_entity(self.root, f"corrupt-{index}", document)
            self.assertFalse(result.valid)
            self.assertEqual(result.reason_code, "artifact_image_invalid")

    def test_oversized_png_dimensions_fail_before_pixel_decode(self) -> None:
        content = bytearray(PNG)
        content[16:24] = struct.pack(">II", 100_000, 100_000)
        content[29:33] = struct.pack(
            ">I", binascii.crc32(bytes(content[12:29])) & 0xFFFFFFFF
        )
        oversized = bytes(content)
        path = self.root / "oversized.png"
        path.write_bytes(oversized)
        document = {
            "relativePath": path.name,
            "contentType": "image/png",
            "sizeBytes": len(oversized),
            "hash": hashlib.sha256(oversized).hexdigest(),
        }
        result = verify_artifact_entity(self.root, "oversized", document)
        self.assertFalse(result.valid)
        self.assertEqual(result.reason_code, "artifact_image_dimensions_invalid")

    def test_parent_escape_is_rejected_before_read(self) -> None:
        document = {**self.document, "relativePath": "../proof.png"}

        result = verify_artifact_entity(
            self.root, "opaque-artifact", document
        )

        self.assertFalse(result.valid)
        self.assertEqual(result.reason_code, "artifact_path_invalid")


if __name__ == "__main__":
    unittest.main()
