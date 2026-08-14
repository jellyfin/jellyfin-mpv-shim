"""The import-table guard, against PE files built to have the answer in them.

``tools/check_win_libmpv_deps.py`` is what makes raising the pinned mpv release
safe: it reads libmpv's import table and fails the build on a dependency
Windows does not have. It only earns that if it reads the table correctly, and
the file it reads in CI is a 120 MB binary nothing here can carry -- so the
fixtures are synthesized, with the imports chosen by the test.

The three things worth getting wrong, and therefore worth pinning:

* **Hard versus delay-loaded.** They are separate directories with different
  record layouts, and only the first is fatal. A checker that merges them
  fails a build over a dependency that costs nothing until it is used.
* **PE32 versus PE32+.** The data directories sit at a different offset in
  each, and this project ships a 32-bit installer as well as a 64-bit one.
  Reading the wrong offset does not raise -- it finds no imports and passes.
* **The API set stubs.** ``api-ms-win-crt-*`` and friends are contract names
  resolved by the loader, not files; a checker that demands them by name
  rejects every real libmpv.
"""

import contextlib
import io
import struct
import unittest

from tools.check_win_libmpv_deps import (
    NotAPeFile,
    imported_dlls,
    is_system_dll,
    main,
)

SECTION_RVA = 0x1000
SECTION_RAW = 0x400


def build_pe(hard=(), delayed=(), magic=0x20B) -> bytes:
    """A PE image with exactly the import descriptors asked for.

    Minimal but structurally real: a DOS stub, a COFF header, an optional
    header of the requested kind, sixteen data directories and one section
    holding both import directories and their name strings.
    """
    blob = bytearray()
    names = {}

    def name_rva(name: str) -> int:
        if name not in names:
            names[name] = SECTION_RVA + len(blob)
            blob.extend(name.encode("ascii") + b"\0")
        return names[name]

    # Lay the strings down first so the descriptors can point back at them.
    for name in list(hard) + list(delayed):
        name_rva(name)

    import_rva = SECTION_RVA + len(blob)
    for name in hard:
        # IMAGE_IMPORT_DESCRIPTOR: the name is the third of five dwords.
        blob.extend(struct.pack("<IIIII", 0, 0, 0, names[name], 0))
    blob.extend(b"\0" * 20)
    import_size = SECTION_RVA + len(blob) - import_rva

    delay_rva = SECTION_RVA + len(blob)
    for name in delayed:
        # ImgDelayDescr: attributes, then the name, then six more dwords.
        blob.extend(struct.pack("<IIIIIIII", 1, names[name], 0, 0, 0, 0, 0, 0))
    blob.extend(b"\0" * 32)
    delay_size = SECTION_RVA + len(blob) - delay_rva

    directories = [(0, 0)] * 16
    directories[1] = (import_rva, import_size)
    directories[13] = (delay_rva, delay_size)

    optional_head = 112 if magic == 0x20B else 96
    optional_size = optional_head + 16 * 8

    image = bytearray(b"MZ" + b"\0" * 0x3E)
    struct.pack_into("<I", image, 0x3C, 0x40)
    image.extend(b"PE\0\0")
    image.extend(struct.pack(
        "<HHIIIHH",
        0x8664 if magic == 0x20B else 0x014C,  # Machine
        1,                                     # NumberOfSections
        0, 0, 0,                               # timestamps / symbol table
        optional_size,
        0x2022,                                # Characteristics: DLL
    ))

    optional = bytearray(b"\0" * optional_size)
    struct.pack_into("<H", optional, 0, magic)
    for index, (rva, size) in enumerate(directories):
        struct.pack_into("<II", optional, optional_head + index * 8, rva, size)
    image.extend(optional)

    image.extend(struct.pack(
        "<8sIIII IIHHI",
        b".rdata",
        len(blob),      # VirtualSize
        SECTION_RVA,    # VirtualAddress
        len(blob),      # SizeOfRawData
        SECTION_RAW,    # PointerToRawData
        0, 0, 0, 0, 0,  # relocations, line numbers, characteristics
    ))

    image.extend(b"\0" * (SECTION_RAW - len(image)))
    image.extend(blob)
    return bytes(image)


class ImportTableTest(unittest.TestCase):
    def _write(self, data: bytes) -> str:
        import os
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".dll")
        os.write(handle, data)
        os.close(handle)
        self.addCleanup(os.unlink, path)
        return path

    def test_reads_the_hard_imports(self):
        path = self._write(build_pe(hard=["KERNEL32.dll", "vulkan-1.dll"]))
        hard, delayed = imported_dlls(path)
        self.assertEqual(hard, ["KERNEL32.dll", "vulkan-1.dll"])
        self.assertEqual(delayed, [])

    def test_keeps_the_delay_imports_apart(self):
        path = self._write(
            build_pe(hard=["KERNEL32.dll"], delayed=["vulkan-1.dll"])
        )
        hard, delayed = imported_dlls(path)
        self.assertEqual(hard, ["KERNEL32.dll"])
        self.assertEqual(delayed, ["vulkan-1.dll"])

    def test_reads_a_32_bit_image(self):
        # The directories move by 16 bytes between PE32 and PE32+, and the
        # 32-bit installer is built from a real i686 libmpv. Reading the
        # wrong offset yields no imports, which is a silent pass.
        path = self._write(build_pe(hard=["vulkan-1.dll"], magic=0x10B))
        hard, _ = imported_dlls(path)
        self.assertEqual(hard, ["vulkan-1.dll"])

    def test_refuses_something_that_is_not_a_pe(self):
        path = self._write(b"this is not a dll")
        with self.assertRaises(NotAPeFile):
            imported_dlls(path)


class SystemDllTest(unittest.TestCase):
    def test_matches_regardless_of_case(self):
        # Real import tables mix them: IPHLPAPI.DLL next to KERNEL32.dll.
        self.assertTrue(is_system_dll("KERNEL32.dll"))
        self.assertTrue(is_system_dll("IPHLPAPI.DLL"))
        self.assertTrue(is_system_dll("kernel32.DLL"))

    def test_accepts_the_api_set_stubs(self):
        self.assertTrue(is_system_dll("api-ms-win-crt-stdio-l1-1-0.dll"))
        self.assertTrue(is_system_dll("ext-ms-win-ntuser-window-l1-1-0.dll"))

    def test_does_not_accept_the_vulkan_loader(self):
        self.assertFalse(is_system_dll("vulkan-1.dll"))


class ExitCodeTest(unittest.TestCase):
    def _write(self, data: bytes) -> str:
        import os
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".dll")
        os.write(handle, data)
        os.close(handle)
        self.addCleanup(os.unlink, path)
        return path

    def _run(self, argv) -> int:
        # It is a CI script and reports on both streams; keep that out of
        # the suite's own output.
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main(argv)

    def test_passes_a_libmpv_that_needs_only_windows(self):
        path = self._write(build_pe(hard=[
            "KERNEL32.dll", "api-ms-win-crt-heap-l1-1-0.dll", "OPENGL32.dll",
        ]))
        self.assertEqual(self._run([path]), 0)

    def test_fails_the_vulkan_regression(self):
        path = self._write(build_pe(hard=["KERNEL32.dll", "vulkan-1.dll"]))
        self.assertEqual(self._run([path]), 1)

    def test_a_delay_loaded_dependency_is_not_a_failure(self):
        # This is the version of the same import that would have been fine:
        # resolved on first use, so a machine without a Vulkan loader loses
        # the Vulkan output rather than the whole DLL.
        path = self._write(
            build_pe(hard=["KERNEL32.dll"], delayed=["vulkan-1.dll"])
        )
        self.assertEqual(self._run([path]), 0)

    def test_allow_accepts_something_shipped_beside_it(self):
        path = self._write(build_pe(hard=["vulkan-1.dll"]))
        self.assertEqual(self._run(["--allow", "vulkan-1.dll", path]), 0)

    def test_a_file_it_cannot_read_is_a_failure(self):
        path = self._write(b"not a pe file at all")
        self.assertEqual(self._run([path]), 1)


if __name__ == "__main__":
    unittest.main()
