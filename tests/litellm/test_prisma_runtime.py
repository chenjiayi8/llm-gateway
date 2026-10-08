"""Native smoke checks run during the Wolfi proxy image build, without a database."""

import ctypes
import os
import platform
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(
    os.environ.get("LITELLM_PRISMA_RUNTIME_SMOKE") == "1",
    "Requires the built proxy image and its generated Prisma binaries",
)
class TestPrismaRuntime(unittest.TestCase):
    def test_openssl_3_libraries_load(self):
        for library in ("libssl.so.3", "libcrypto.so.3"):
            with self.subTest(library=library):
                ctypes.CDLL(library)

    def test_native_query_engine_starts(self):
        binary_names = {
            "x86_64": "query-engine-debian-openssl-3.0.x",
            "aarch64": "query-engine-linux-arm64-openssl-3.0.x",
        }
        architecture = platform.machine()
        self.assertIn(architecture, binary_names, "Unsupported native Prisma smoke-test architecture")
        cache = Path.home() / ".cache" / "prisma-python" / "binaries"
        engines = sorted(path for path in cache.rglob(binary_names[architecture]) if path.is_file())
        self.assertTrue(engines, "prisma generate must install the OpenSSL 3 native query engine")
        result = subprocess.run(
            [str(engines[0]), "--version"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip(), "Native query engine returned no version")


if __name__ == "__main__":
    unittest.main()
