"""Offline synthetic Pages fixtures; never read a real credential source."""
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notion_credentials as nc
import notion_sync as ns

# Synthetic parser text, not an issued credential.
FIXTURE_TOKEN = "ntn_" + "SYNTHETIC_NOT_A_REAL_CREDENTIAL_" * 2


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def literal(data, width=None):
    size = len(data) - 1
    if width is None and size < 60:
        tag = bytes([size << 2])
    else:
        width = width or max(1, (size.bit_length() + 7) // 8)
        tag = bytes([(59 + width) << 2]) + size.to_bytes(width, "little")
    return tag + data


def frame(raw):
    block = varint(len(raw)) + literal(raw) if raw else b"\0"
    return b"\0" + len(block).to_bytes(3, "little") + block


class SnappyTests(unittest.TestCase):
    def test_short_and_multibyte_preamble_literals(self):
        for raw in (b"hello", b"z" * 400):
            with self.subTest(size=len(raw)):
                self.assertEqual(nc.decompress_snappy(varint(len(raw)) + literal(raw)), raw)

    def test_all_long_literal_length_widths(self):
        raw = b"x" * 64
        for width in range(1, 5):
            with self.subTest(width=width):
                self.assertEqual(nc.decompress_snappy(varint(len(raw)) + literal(raw, width)), raw)

    def test_copy_one_byte_offset_with_overlap(self):
        # Literal xab, then length 4 offset 2 => xababab.
        self.assertEqual(nc.decompress_snappy(b"\x07\x08xab\x01\x02"), b"xababab")

    def test_copy_one_byte_offset_high_bits(self):
        raw = bytes(range(256)) + b"abcd"
        # 260 has low byte 4 and high 3 offset bits in tag.
        block = varint(264) + literal(raw) + bytes([0x21, 4])
        self.assertEqual(nc.decompress_snappy(block), raw + raw[:4])

    def test_copy_two_byte_offset_with_overlap(self):
        self.assertEqual(nc.decompress_snappy(b"\x07\x08xab\x0e\x02\x00"), b"xababab")

    def test_copy_four_byte_offset_and_maximum_overlap(self):
        # Literal A, then copy 64 bytes at offset 1.
        block = b"\x41\x00A\xff\x01\x00\x00\x00"
        self.assertEqual(nc.decompress_snappy(block), b"A" * 65)

    def test_empty_raw_block(self):
        self.assertEqual(nc.decompress_snappy(b"\0"), b"")

    def test_rejects_truncated_preamble_and_invalid_32bit_lengths(self):
        for block in (b"", b"\x80", b"\x80" * 5, b"\xff\xff\xff\xff\x10"):
            with self.subTest(size=len(block)), self.assertRaisesRegex(ValueError, "invalid credential document"):
                nc.decompress_snappy(block)

    def test_rejects_truncated_literal_headers_and_payloads(self):
        for block in (b"\x02\xf0", b"\x02\xf4\x01", b"\x02\xf8\x01\0",
                      b"\x02\xfc\x01\0\0", b"\x03\x08ab"):
            with self.subTest(block_size=len(block)), self.assertRaises(ValueError):
                nc.decompress_snappy(block)

    def test_rejects_truncated_copy_operands(self):
        for operand in (b"\x01", b"\x02\x01", b"\x03\x01\0\0"):
            with self.subTest(width=len(operand)), self.assertRaises(ValueError):
                nc.decompress_snappy(b"\x05\x00a" + operand)

    def test_rejects_zero_and_out_of_range_offsets(self):
        for offset in (0, 2):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                nc.decompress_snappy(b"\x05\x00a\x01" + bytes([offset]))

    def test_rejects_output_underflow_overflow_and_limit_before_allocation(self):
        for block in (b"\x03\x04ab", b"\x01\x04ab", b"\x02\x00a\x01\x01"):
            with self.subTest(size=len(block)), self.assertRaises(ValueError):
                nc.decompress_snappy(block)
        with self.assertRaises(ValueError):
            nc.decompress_snappy(varint(1024 * 1024), limit=16)

    def test_iwa_joins_frames_and_obeys_combined_limit(self):
        raw = frame(b"first") + frame(b"second")
        self.assertEqual(nc.read_iwa(raw, limit=11), b"firstsecond")
        with self.assertRaises(ValueError):
            nc.read_iwa(raw, limit=10)

    def test_iwa_rejects_truncated_header_payload_and_nonzero_kind(self):
        for raw in (b"\0", b"\0\x02\0\0\0", b"\x01\x01\0\0\0", b"\0\0\0\0"):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                nc.read_iwa(raw)

    def test_iwa_frame_count_is_bounded(self):
        with patch.object(nc, "MAX_FRAMES", 2), self.assertRaises(ValueError):
            nc.read_iwa(frame(b"") * 3)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "selected.pages"
        self.fallback = Mock(return_value="SYNTHETIC_KEYCHAIN_VALUE")

    def write_pages(self, raw, extra=None):
        with zipfile.ZipFile(self.source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(nc.DOCUMENT_MEMBER, frame(raw))
            for name, value in (extra or {}).items():
                archive.writestr(name, value)
        return self.source

    def settings(self, value):
        (self.root / "app-settings.json").write_text(json.dumps(value), encoding="utf-8")

    def test_unique_document_candidate_ignores_other_archive_members(self):
        other = b"secret_" + b"OTHER_SYNTHETIC_VALUE_" * 2
        self.write_pages(b"text\0" + FIXTURE_TOKEN.encode() + b"\0end",
                         {"preview.txt": other, "Index/Other.iwa": frame(other)})
        self.assertEqual(nc.pages_token(self.source), FIXTURE_TOKEN)

    def test_repeated_same_candidate_is_unique(self):
        self.write_pages((FIXTURE_TOKEN + "\n" + FIXTURE_TOKEN).encode())
        self.assertEqual(nc.pages_token(self.source), FIXTURE_TOKEN)

    def test_accepts_old_prefix_and_minimum_suffix(self):
        value = "secret_" + "A" * 20
        self.write_pages(value.encode())
        self.assertEqual(nc.pages_token(self.source), value)

    def test_zero_ambiguous_short_and_oversized_candidates_fail_closed(self):
        for raw in (b"ordinary document", b"ntn_" + b"A" * 19,
                    (FIXTURE_TOKEN + "\nsecret_" + "B" * 32).encode(), b"ntn_" + b"A" * 513):
            with self.subTest(size=len(raw)):
                self.write_pages(raw)
                self.assertIsNone(nc.pages_token(self.source))

    def test_selected_file_can_split_candidate_across_iwa_frames(self):
        raw = FIXTURE_TOKEN.encode()
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr(nc.DOCUMENT_MEMBER, frame(raw[:12]) + frame(raw[12:]))
        self.assertEqual(nc.pages_token(self.source), FIXTURE_TOKEN)

    def test_rejects_missing_wrong_extension_relative_directory_and_nonzip(self):
        self.source.write_bytes(b"not a ZIP archive")
        for path in (self.source, self.root / "missing.pages", self.root,
                     Path("selected.pages"), self.root / "selected.txt"):
            with self.subTest(path_kind=path.suffix):
                self.assertIsNone(nc.pages_token(path))

    def test_rejects_missing_duplicate_or_malformed_document_member(self):
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("preview.txt", FIXTURE_TOKEN)
        self.assertIsNone(nc.pages_token(self.source))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.source, "w") as archive:
                archive.writestr(nc.DOCUMENT_MEMBER, frame(FIXTURE_TOKEN.encode()))
                archive.writestr(nc.DOCUMENT_MEMBER, frame(FIXTURE_TOKEN.encode()))
        self.assertIsNone(nc.pages_token(self.source))
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr(nc.DOCUMENT_MEMBER, b"\0\x02\0\0\0")
        self.assertIsNone(nc.pages_token(self.source))

    def test_archive_member_and_decompressed_limits(self):
        self.write_pages(FIXTURE_TOKEN.encode())
        for name, limit in (("MAX_ARCHIVE_BYTES", 1), ("MAX_DOCUMENT_BYTES", 1),
                            ("MAX_ARCHIVE_MEMBERS", 0)):
            with self.subTest(limit=name), patch.object(nc, name, limit):
                self.assertIsNone(nc.pages_token(self.source))
        with patch.object(nc, "MAX_DECOMPRESSED_BYTES", 1):
            self.assertIsNone(nc.pages_token(self.source))

    def test_corrupted_zip_compressed_data_fails_without_exception(self):
        self.write_pages(FIXTURE_TOKEN.encode())
        with zipfile.ZipFile(self.source) as archive:
            info = archive.getinfo(nc.DOCUMENT_MEMBER)
            start = info.header_offset + 30 + len(info.filename.encode("utf-8")) + len(info.extra)
        raw = bytearray(self.source.read_bytes())
        raw[start] = 7  # Raw Deflate reserved block type, before any document text.
        self.source.write_bytes(raw)
        self.assertIsNone(nc.pages_token(self.source))

    def test_failures_print_no_paths_candidate_or_traceback(self):
        self.source.write_bytes(FIXTURE_TOKEN.encode())
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertIsNone(nc.pages_token(self.source))
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")

    def test_missing_unset_and_null_setting_use_keychain(self):
        for settings in (None, {}, {"notion_token_source_file": None}):
            with self.subTest(setting=settings):
                if settings is not None:
                    self.settings(settings)
                self.assertEqual(nc.configured_token(self.root, self.fallback), "SYNTHETIC_KEYCHAIN_VALUE")
        self.assertEqual(self.fallback.call_count, 3)

    def test_selected_pages_credential_never_reads_keychain(self):
        self.write_pages(FIXTURE_TOKEN.encode())
        self.settings({"notion_token_source_file": str(self.source)})
        self.assertEqual(nc.configured_token(self.root, self.fallback), FIXTURE_TOKEN)
        self.fallback.assert_not_called()

    def test_invalid_configured_source_never_falls_back(self):
        for source in ("", False, 42, [], {}, "relative.pages", str(self.root / "missing.pages")):
            with self.subTest(source_type=type(source).__name__):
                self.settings({"notion_token_source_file": source})
                self.assertIsNone(nc.configured_token(self.root, self.fallback))
        self.fallback.assert_not_called()

    def test_ambiguous_selected_source_never_falls_back(self):
        self.write_pages((FIXTURE_TOKEN + "\nsecret_" + "B" * 32).encode())
        self.settings({"notion_token_source_file": str(self.source)})
        self.assertIsNone(nc.configured_token(self.root, self.fallback))
        self.fallback.assert_not_called()

    def test_malformed_oversized_or_nonobject_settings_never_fall_back(self):
        path = self.root / "app-settings.json"
        for raw in (b"{", b"[]", b"null", b"\xff"):
            with self.subTest(size=len(raw)):
                path.write_bytes(raw)
                self.assertIsNone(nc.configured_token(self.root, self.fallback))
        self.settings({})
        with patch.object(nc, "MAX_SETTINGS_BYTES", 1):
            self.assertIsNone(nc.configured_token(self.root, self.fallback))
        self.fallback.assert_not_called()

    def test_workspace_default_provider_observes_file_setting_without_keychain(self):
        self.write_pages(FIXTURE_TOKEN.encode())
        workspace = ns.NotionWorkspace(self.root)
        self.settings({"notion_token_source_file": str(self.source)})
        with patch.object(ns, "keychain_token", side_effect=AssertionError("unexpected Keychain access")):
            self.assertEqual(workspace.token_provider(), FIXTURE_TOKEN)
            self.source.write_bytes(b"broken ZIP")
            self.assertIsNone(workspace.token_provider())

    def test_workspace_default_provider_retains_scoped_keychain_when_unset(self):
        workspace = ns.NotionWorkspace(self.root)
        with patch.object(ns, "keychain_token", self.fallback):
            self.assertEqual(workspace.token_provider(), "SYNTHETIC_KEYCHAIN_VALUE")
        self.fallback.assert_called_once_with()

    def test_workspace_explicit_injected_provider_is_unchanged(self):
        self.settings({"notion_token_source_file": str(self.root / "missing.pages")})
        provider = Mock(return_value="SYNTHETIC_INJECTED_VALUE")
        workspace = ns.NotionWorkspace(self.root, token_provider=provider)
        self.assertIs(workspace.token_provider, provider)
        self.assertEqual(workspace.token_provider(), "SYNTHETIC_INJECTED_VALUE")

    def test_documented_package_import_and_providers_in_fresh_process(self):
        self.write_pages(FIXTURE_TOKEN.encode())
        script = """
import json
from pathlib import Path
import sys
from unittest.mock import patch
from widget.notion_sync import NotionWorkspace
root, source = map(Path, sys.argv[1:])
provider = lambda: 'INJECTED_PACKAGE_FIXTURE'
assert NotionWorkspace(root, token_provider=provider).token_provider is provider
workspace = NotionWorkspace(root)
with patch('widget.notion_sync.keychain_token', return_value='SCOPED_PACKAGE_FIXTURE') as lookup:
    assert workspace.token_provider() == 'SCOPED_PACKAGE_FIXTURE'
    lookup.assert_called_once_with()
(root / 'app-settings.json').write_text(json.dumps({'notion_token_source_file': str(source)}))
with patch('widget.notion_sync.keychain_token', side_effect=AssertionError('Unexpected credential lookup')):
    assert workspace.token_provider().startswith('ntn_SYNTHETIC_NOT_A_REAL_CREDENTIAL_')
"""
        result = subprocess.run([sys.executable, "-E", "-c", script, str(self.root), str(self.source)],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
