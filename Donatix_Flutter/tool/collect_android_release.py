#!/usr/bin/env python3
"""Verify actual release binaries before exposing stable Codemagic artifacts.

Uses installed Android build-tools and the JDK. No credentials, HTTP requests,
Flutter invocation or signing-key copies are needed here.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def elf_alignment(data: bytes, name: str) -> list[int]:
    if len(data) < 64 or data[:4] != b'\x7fELF' or data[4] != 2 or data[5] != 1:
        raise ValueError(f'{name}: expected a 64-bit little-endian ELF library')
    offset = struct.unpack_from('<Q', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 54)
    if size < 56 or count == 0 or offset + size * count > len(data):
        raise ValueError(f'{name}: malformed ELF program headers')
    alignments, writable, relro_ends = [], [], []
    for i in range(count):
        kind, flags, file_offset, virtual, _, _, memory_size, alignment = struct.unpack_from('<IIQQQQQQ', data, offset + i * size)
        if kind == 1:
            if alignment < 16384 or alignment & (alignment - 1) or file_offset % alignment != virtual % alignment:
                raise ValueError(f'{name}: LOAD segment is not aligned for 16 KB pages')
            alignments.append(alignment)
            if flags & 2:
                writable.append((virtual, virtual + memory_size))
        if kind == 0x6474e552:
            relro_ends.append(virtual + memory_size)
    # Rounding RELRO protection up to 16 KB must not cover live writable data.
    # An unused tail after LOAD's memory boundary is safe, including in the
    # official Flutter engine. Also check other LOAD segments on that page.
    for end in relro_ends:
        protected_end = (end + 16383) // 16384 * 16384
        if any(start < protected_end and stop > end for start, stop in writable):
            raise ValueError(f'{name}: RELRO shares a 16 KB page with mutable LOAD data')
    if not alignments:
        raise ValueError(f'{name}: no LOAD segments')
    return alignments


def inspect_archive(path: Path, bundle=False) -> list[dict]:
    prefix = 'base/lib/' if bundle else 'lib/'
    checked = []
    with zipfile.ZipFile(path) as archive:
        broken = archive.testzip()
        if broken:
            raise ValueError(f'{path.name}: damaged entry {broken}')
        if bundle:
            names = archive.namelist()
            if 'BundleConfig.pb' not in names or 'base/manifest/AndroidManifest.xml' not in names:
                raise ValueError('Not an Android App Bundle')
            if not any(n.startswith('META-INF/') and n.endswith('.SF') for n in names):
                raise ValueError('AAB has no JAR signature')
        for entry in archive.infolist():
            if not entry.filename.startswith(prefix) or not entry.filename.endswith('.so'):
                continue
            abi = entry.filename[len(prefix):].split('/')[0]
            if abi not in ('arm64-v8a', 'x86_64'):
                continue
            data = archive.read(entry)
            checked.append({'library': entry.filename, 'load_alignment': elf_alignment(data, entry.filename)})
    if not any('/arm64-v8a/libflutter.so' in item['library'] for item in checked):
        raise ValueError(f'{path.name}: arm64 Flutter engine is missing')
    return checked


def build_tools() -> Path:
    roots = [os.environ.get('ANDROID_SDK_ROOT'), os.environ.get('ANDROID_HOME')]
    roots += [str(Path.home() / 'Library/Android/sdk'), '/opt/android-sdk']
    candidates = []
    for root in filter(None, roots):
        base = Path(root) / 'build-tools'
        if base.is_dir():
            for folder in base.iterdir():
                if re.fullmatch(r'\d+\.\d+\.\d+', folder.name) and int(folder.name.split('.')[0]) >= 35:
                    if all((folder / name).is_file() for name in ('apksigner', 'zipalign', 'aapt')):
                        candidates.append(folder)
    if not candidates:
        raise ValueError('Android SDK build-tools 35+ with apksigner, zipalign and aapt are required')
    return max(candidates, key=lambda p: tuple(map(int, p.name.split('.'))))


def run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(f'{Path(command[0]).name} verification failed:\n{result.stdout}\n{result.stderr}')
    return result.stdout + result.stderr


def protobuf_fields(raw: bytes):
    offset = 0
    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(raw):
                raise ValueError('Truncated BundleConfig protobuf')
            byte = raw[offset]; offset += 1
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise ValueError('Invalid BundleConfig protobuf varint')
    while offset < len(raw):
        tag = varint(); wire = tag & 7; number = tag >> 3
        if number == 0:
            raise ValueError('Invalid protobuf field number')
        if wire == 0:
            value = varint()
        elif wire == 2:
            size = varint(); value = raw[offset:offset + size]; offset += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4; value = raw[offset:offset + size]; offset += size
        else:
            raise ValueError('Unsupported BundleConfig protobuf wire type')
        if offset > len(raw):
            raise ValueError('Truncated BundleConfig protobuf field')
        yield number, value


def bundle_alignment(path: Path) -> str:
    # Official schema: google/bundletool src/main/proto/config.proto.
    with zipfile.ZipFile(path) as z:
        bundle = dict(protobuf_fields(z.read('BundleConfig.pb')))
    optimizations = dict(protobuf_fields(bundle.get(2, b'')))
    native = dict(protobuf_fields(optimizations.get(2, b'')))
    if not native.get(1, 0):
        return 'compressed native libraries'
    if native.get(2) not in (2, 3):
        raise ValueError('AAB does not request 16 KB or 64 KB page alignment')
    return '16 KB' if native[2] == 2 else '64 KB'


def collect(apk: Path, aab: Path, output: Path) -> dict:
    destinations = [output / name for name in (
        'Donatix-release.apk', 'Donatix-release.aab', 'release-verification.json')]
    if any(path.resolve() in (apk.resolve(), aab.resolve()) for path in destinations):
        raise ValueError('Release inputs must be outside the collection output directory')
    # A failed verification must not expose artifacts left by a previous run.
    for path in destinations:
        path.unlink(missing_ok=True)
    for path in (apk, aab):
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Release binary not found: {path}')
    tools = build_tools()
    signature = run([str(tools / 'apksigner'), 'verify', '--verbose', '--print-certs', str(apk)])
    certificates = re.findall(r'certificate SHA-256 digest: ([0-9a-fA-F]+)', signature)
    if not certificates or 'CN=Android Debug' in signature:
        raise ValueError('APK release signature is missing or uses an Android debug certificate')
    run([str(tools / 'zipalign'), '-c', '-P', '16', '4', str(apk)])
    badging = run([str(tools / 'aapt'), 'dump', 'badging', str(apk)])
    package = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'", badging)
    target = re.search(r"targetSdkVersion:'(\d+)'", badging)
    if not package or package[1] != 'tj.donatix.app' or 'application-debuggable' in badging:
        raise ValueError('Expected a non-debuggable tj.donatix.app release APK')
    if not target or int(target[1]) < 35:
        raise ValueError('APK targetSdk must be 35 or newer')
    libraries = {'apk': inspect_archive(apk), 'aab': inspect_archive(aab, bundle=True)}
    aab_alignment = bundle_alignment(aab)
    jar = run(['jarsigner', '-J-Duser.language=en', '-J-Duser.country=US', '-verify', str(aab)])
    if 'jar verified.' not in jar.lower():
        raise ValueError('AAB JAR signature could not be verified')
    aab_certificate = run(['keytool', '-J-Duser.language=en', '-J-Duser.country=US', '-printcert', '-jarfile', str(aab)])
    aab_digests = re.findall(r'SHA256: ([0-9A-Fa-f:]+)', aab_certificate)
    if not aab_digests or aab_digests[0].replace(':', '').lower() != certificates[0].lower():
        raise ValueError('APK and AAB were signed with different certificates')
    report = {
        'verified_at_utc': datetime.now(timezone.utc).isoformat(),
        'package': package[1], 'version_code': int(package[2]), 'version_name': package[3],
        'target_sdk': int(target[1]), 'apk_certificate_sha256': certificates,
        'apk_signature_verified': True, 'aab_signature_verified': True,
        'apk_zip_alignment_16kb_verified': True,
        'aab_native_libraries_alignment': aab_alignment, 'libraries': libraries,
        'device_acceptance': 'Requires installation and live operations on an Android device',
        'artifacts': [],
    }
    output.mkdir(parents=True, exist_ok=True)
    for source, name in ((apk, 'Donatix-release.apk'), (aab, 'Donatix-release.aab')):
        destination = output / name
        shutil.copy2(source, destination)
        report['artifacts'].append({'file': name, 'bytes': destination.stat().st_size,
                                    'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()})
        print(f'Collected {destination}: {destination.stat().st_size} bytes')
    (output / 'release-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apk', type=Path, default=Path('build/app/outputs/flutter-apk/app-release.apk'))
    parser.add_argument('--aab', type=Path, default=Path('build/app/outputs/bundle/release/app-release.aab'))
    parser.add_argument('--output', type=Path, default=Path(os.environ.get('CM_BUILD_DIR', '.')) / 'donatix-release')
    args = parser.parse_args()
    try:
        collect(args.apk, args.aab, args.output)
    except (ValueError, OSError, zipfile.BadZipFile) as error:
        parser.exit(1, f'Android release verification failed: {error}\n')


if __name__ == '__main__':
    main()
