import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from collect_android_release import elf_alignment, inspect_archive, collect, bundle_alignment, protobuf_fields


def elf(*headers):
    data = bytearray(64 + 56 * len(headers))
    data[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HH', data, 54, 56, len(headers))
    for i, header in enumerate(headers):
        struct.pack_into('<IIQQQQQQ', data, 64 + i * 56, *header)
    return bytes(data)


class ReleaseArtifactChecks(unittest.TestCase):
    def test_rejects_4kb_and_malformed_libraries(self):
        for raw in [b'not-elf', elf((1, 4, 0, 0, 0, 100, 100, 4096))]:
            with self.assertRaises(ValueError):
                elf_alignment(raw, 'fixture.so')

    def test_supports_16kb_and_larger_load_alignment(self):
        raw = elf((1, 4, 0, 0, 0, 100, 100, 16384), (1, 6, 0, 65536, 0, 100, 100, 65536))
        self.assertEqual(elf_alignment(raw, 'fixture.so'), [16384, 65536])

    def test_detects_relro_protecting_live_writable_tail(self):
        raw = elf((1, 6, 0, 0, 0, 8192, 8192, 16384), (0x6474e552, 4, 0, 0, 0, 4096, 4096, 1))
        with self.assertRaisesRegex(ValueError, 'mutable'):
            elf_alignment(raw, 'fixture.so')

    def test_relro_ending_at_load_boundary_does_not_protect_mutable_data(self):
        raw = elf((1, 6, 0, 0, 0, 4096, 4096, 65536), (0x6474e552, 4, 0, 0, 0, 4096, 4096, 1))
        self.assertEqual(elf_alignment(raw, 'fixture.so'), [65536])

    def test_relro_cannot_protect_a_second_load_on_the_same_page(self):
        raw = elf((1, 6, 0, 0, 0, 4096, 4096, 16384),
                  (1, 6, 8192, 8192, 0, 1024, 1024, 16384),
                  (0x6474e552, 4, 0, 0, 0, 4096, 4096, 1))
        with self.assertRaisesRegex(ValueError, 'mutable'):
            elf_alignment(raw, 'fixture.so')

    def test_bundle_alignment_requires_explicit_16kb_for_uncompressed_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.aab'
            for enabled, alignment, expected in [(1, 2, '16 KB'), (1, 3, '64 KB'),
                                                  (0, 0, 'compressed native libraries'), (1, 1, None)]:
                native = bytes([8, enabled, 16, alignment])
                optimizations = bytes([18, len(native)]) + native
                config = bytes([18, len(optimizations)]) + optimizations
                with zipfile.ZipFile(path, 'w') as z:
                    z.writestr('BundleConfig.pb', config)
                if expected is None:
                    with self.assertRaisesRegex(ValueError, 'alignment'):
                        bundle_alignment(path)
                else:
                    self.assertEqual(bundle_alignment(path), expected)

    def test_rejects_truncated_or_invalid_bundle_protobuf(self):
        for raw in [b'\x12\x10missing', b'\x00', b'\x08\x80', b'\x0b']:
            with self.assertRaises(ValueError):
                list(protobuf_fields(raw))

    def test_rejects_unsigned_bundle_and_absent_arm64_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'unsigned.aab'
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('BundleConfig.pb', b'fixture')
                z.writestr('base/manifest/AndroidManifest.xml', b'fixture')
            with self.assertRaisesRegex(ValueError, 'signature'):
                inspect_archive(path, bundle=True)
            with self.assertRaisesRegex(ValueError, 'arm64'):
                inspect_archive(path)

    def test_does_not_create_artifacts_when_binaries_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'not found'):
                collect(root / 'missing.apk', root / 'missing.aab', root / 'output')
            self.assertFalse((root / 'output').exists())

    def test_failed_verification_removes_stale_collected_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / 'output'
            output.mkdir()
            for name in ('Donatix-release.apk', 'Donatix-release.aab', 'release-verification.json'):
                (output / name).write_text('old build')
            with self.assertRaisesRegex(ValueError, 'not found'):
                collect(root / 'missing.apk', root / 'missing.aab', output)
            self.assertEqual(list(output.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
