import hashlib
import os
from pathlib import Path
import re
import unittest


QA_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_VARIABLE = "YEYU_QA_REFERENCE_SOURCE_ROOT"
REFERENCE_ROOT = Path(os.environ.get(REFERENCE_VARIABLE, str(QA_ROOT)))
QA_HASHES = {
    "scripts/Build-YeYuGamerWwAdapter.ps1": "947b1b9cd4bad244d8b7eddca14f36a8fe5a432ea9dba3c1db88aff40760c813",
    "scripts/Publish-YeYuGamerLocalRelease.ps1": "91ad7702178fb96a239373ae2b463b89341531cf45efbcfd4469b9f3e925a365",
    "scripts/Prepare-WwOfficialCandidate.ps1": "9eaa7594d02b568d20e4ad72bf00b0e50e1addf109b462a7bfb1adf4fc89928b",
    "integration-patches/ww-binding-followup.patch": "9386ed40aac68cd83ae56f063843e760aa4b98d1d4b140f0014c0895f075b38c",
}
REFERENCE_HASHES = {
    "scripts/Publish-YeYuGamerLocalRelease.ps1": "213f885f7f9377f9ff88c316c437e45580bc4f71dc5c4fa9b4e08cd48a86ec23",
}


def read_pinned(root, relative, expected):
    if not root.is_absolute():
        raise AssertionError("Source root must be absolute")
    try:
        payload = (root / relative).read_bytes()
    except OSError:
        raise AssertionError(f"Missing source: {relative}") from None
    if hashlib.sha256(payload).hexdigest() != expected:
        raise AssertionError(f"SHA256 drift: {relative}; re-review evidence before repinning")
    return payload.decode("utf-8-sig")


class WwBindingCandidateTests(unittest.TestCase):
    def source(self, relative):
        return read_pinned(QA_ROOT, relative, QA_HASHES[relative])

    def test_runtime_default_is_not_ww124_or_source_checkout(self):
        source = self.source("scripts/Build-YeYuGamerWwAdapter.ps1")
        match = re.search(r"\[string\]\$ToolRoot = '([^']+)'", source)
        self.assertTrue(match is not None, "QA builder ToolRoot parameter missing")
        self.assertTrue(match.group(1) == r"D:\Game\Tools\Automation\ok-ww", "QA runtime default changed")
        self.assertTrue("$tool = [IO.Path]::GetFullPath($ToolRoot)" in source, "QA ToolRoot resolution missing")
        self.assertTrue("root=$tool; bindingKind=$wwBindingKind" in source, "QA generated root mapping missing")

    def test_source_candidate_layout_remains_distinct(self):
        source = self.source("scripts/Build-YeYuGamerWwAdapter.ps1")
        for relative in (r"WW\runtime-preparation.json", r"WW\source", r"r\Scripts\pythonw.exe"):
            self.assertTrue(relative in source, f"QA layout requirement missing: {relative}")
        self.assertTrue("WW official source candidate manifest does not match its fixed entry layout." in source,
                        "QA source manifest rejection missing")

    def test_qa_publisher_gap_is_not_reference_success(self):
        source = self.source("scripts/Publish-YeYuGamerLocalRelease.ps1")
        self.assertTrue("Build-YeYuGamerWwAdapter.ps1" in source, "QA canonical builder call missing")
        self.assertFalse("$WwCandidateToolRoot" in source, "QA publisher baseline changed; reassess known gap")
        self.assertFalse("Sync-YeYuGamerCandidatePaths" in source, "QA publisher baseline changed; reassess sync gap")

    def test_prepare_entry_still_rejects_unqualified_target_revision(self):
        source = self.source("scripts/Prepare-WwOfficialCandidate.ps1")
        allowed = re.search(r"\[ValidateSet\(([^\n]+)\)\]", source)
        self.assertTrue(allowed is not None, "QA fixed revision allowlist missing")
        self.assertFalse("1c3dc74ef985c09a9b26a4255a459d6df5d3333f" in allowed.group(1),
                         "Target revision admission changed; review qualification")
        self.assertTrue("official-source.zip" in source, "QA archive output missing")
        self.assertTrue(r"WW\runtime-preparation.json" in source, "QA preparation manifest output missing")

    def test_patch_is_documentation_only_with_explicit_base(self):
        patch = self.source("integration-patches/ww-binding-followup.patch")
        self.assertTrue(bool(re.search(r"(?m)^# baseSHA256 docs/integrations/ok-ww.md: [0-9a-f]{64}$", patch)),
                        "Patch base SHA missing")
        self.assertTrue(re.findall(r"(?m)^diff --git (.+)$", patch) == [
            "a/docs/integrations/ok-ww.md b/docs/integrations/ok-ww.md"
        ], "Patch must remain a single documentation hunk")
        self.assertTrue("yeyu-local-20261028" in patch, "Patch target ref missing")
        self.assertFalse("new file mode" in patch, "Patch must not include whole untracked files")


class WwBindingReferenceEvidenceTests(unittest.TestCase):
    def test_reference_publisher_parameter_and_ww_mapping(self):
        if REFERENCE_ROOT.resolve() == QA_ROOT.resolve():
            read_pinned(REFERENCE_ROOT, "scripts/Publish-YeYuGamerLocalRelease.ps1",
                        QA_HASHES["scripts/Publish-YeYuGamerLocalRelease.ps1"])
            self.skipTest("QA HEAD lacks publisher bridge; explicit pinned reference is evidence only")
        relative = "scripts/Publish-YeYuGamerLocalRelease.ps1"
        source = read_pinned(REFERENCE_ROOT, relative, REFERENCE_HASHES[relative])
        for required, reason in (
            ("if ($WwCandidateToolRoot) { $arguments.ToolRoot = $WwCandidateToolRoot }", "reference builder forwarding missing"),
            ("if ('WW' -cin $OpenKuroGameIds -and $WwCandidateToolRoot)", "reference WW scope missing"),
            ("-GameId WW -BindingField toolPath -Path $WwCandidateToolRoot", "reference path mapping missing"),
            ("Sync-YeYuGamerCandidatePaths -RuntimeRoot $runtimeRoot", "reference synchronization call missing"),
        ):
            self.assertTrue(required in source, reason)


if __name__ == "__main__":
    unittest.main()
