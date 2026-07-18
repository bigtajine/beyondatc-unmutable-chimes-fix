#!/usr/bin/env python3
# Silences the two BeyondATC UI chimes that fire on every launch/connect:
#
#   - AudioManager.OnTriggerChimeSfx()   -> chimeSfx.Play()      (splash logo chime)
#   - MainInterface.PanelSequence()      -> connectedBark.Play() ("BeyondATC connected")
#
# Both are one-liner calls into UnityEngine.AudioSource.Play(). Rather than
# hardcoding file offsets (which break on every BeyondATC update), this walks
# the assembly's .NET metadata (via `dnfile`) to find the methods/fields/
# memberref by *name*, then wildcard-scans each method's IL for the
# "push audio-source field, callvirt Play()" instruction sequence and NOPs
# it out. If BeyondATC renames these members or restructures the calls, the
# script will find nothing and abort rather than patch the wrong bytes.
#
# Target: BeyondATC_Data/Managed/Assembly-CSharp.dll
# Usage: python patch_beyondatc_sounds.py <path to Assembly-CSharp.dll>
# Close BeyondATC first. Original is backed up to Assembly-CSharp.dll.bak.
import struct
import shutil
import sys
from pathlib import Path

import dnfile

NOP = 0x00
LDFLD = 0x7B
CALLVIRT = 0x6F
# single-byte "push a reference" opcodes that can precede ldfld here
LOAD_OPCODES = {0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09}  # ldarg.0-3, ldloc.0-3
GAP = 24  # max bytes between the ldfld and its callvirt


def field_token(md, type_name, field_name):
    for i, row in enumerate(md.TypeDef.rows):
        if str(row.TypeName) != type_name:
            continue
        for fi in row.FieldList:
            if str(fi.row.Name) == field_name:
                return 0x04000000 | fi.row_index
    raise LookupError(f"field {type_name}.{field_name} not found")


def audiosource_play_token(md):
    for i, row in enumerate(md.MemberRef.rows, start=1):
        if str(row.Name) != "Play":
            continue
        cls = row.Class.row
        if getattr(cls, "TypeName", None) is not None and str(cls.TypeName) == "AudioSource":
            return 0x0A000000 | i
    raise LookupError("AudioSource::Play memberref not found")


def find_method(md, type_name, method_name):
    for i, row in enumerate(md.TypeDef.rows):
        if str(row.TypeName) != type_name:
            continue
        for mi in row.MethodList:
            if str(mi.row.Name) == method_name:
                return mi.row
    raise LookupError(f"method {type_name}.{method_name} not found")


def method_body_range(pe, rva):
    off = pe.get_offset_from_rva(rva)
    data = pe.__data__
    header = data[off]
    if header & 0x3 == 0x2:  # tiny header
        size = header >> 2
        code_start = off + 1
        return code_start, size
    flags, _max_stack, code_size = struct.unpack_from("<HHI", data, off)
    hdr_size_dwords = (flags >> 12) & 0xF
    code_start = off + hdr_size_dwords * 4
    return code_start, code_size


def find_play_call(code: bytes, field_tok: int, play_tok: int):
    """Locate `<load ref>; ldfld field_tok; callvirt play_tok` in `code`.
    Returns (start, end) byte offsets (end exclusive) covering the load
    instruction through the callvirt, or None."""
    field_bytes = struct.pack("<I", field_tok)
    play_bytes = struct.pack("<I", play_tok)
    needle = bytes([LDFLD]) + field_bytes
    idx = code.find(needle)
    if idx == -1 or idx == 0:
        return None
    call_needle = bytes([CALLVIRT]) + play_bytes
    window = code[idx + 5: idx + 5 + GAP]
    call_off = window.find(call_needle)
    if call_off == -1:
        return None
    load_op = code[idx - 1]
    if load_op not in LOAD_OPCODES:
        return None
    start = idx - 1
    end = idx + 5 + call_off + 5
    return start, end


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <path to Assembly-CSharp.dll>")
        sys.exit(1)

    dll_path = Path(sys.argv[1])
    if not dll_path.is_file():
        print(f"[!] File not found: {dll_path}")
        sys.exit(1)

    pe = dnfile.dnPE(str(dll_path))
    md = pe.net.mdtables

    play_tok = audiosource_play_token(md)
    chime_field_tok = field_token(md, "AudioManager", "chimeSfx")
    bark_field_tok = field_token(md, "MainInterface", "connectedBark")

    targets = [
        ("AudioManager", "OnTriggerChimeSfx", chime_field_tok, "launch chime"),
        ("<PanelSequence>d__198", "MoveNext", bark_field_tok, "connected bark"),
    ]

    patches = []
    already_patched_count = 0
    for type_name, method_name, field_tok, label in targets:
        method = find_method(md, type_name, method_name)
        code_start, code_size = method_body_range(pe, method.Rva)
        code = pe.__data__[code_start:code_start + code_size]
        hit = find_play_call(code, field_tok, play_tok)
        if hit is None:
            if bytes([NOP]) * 11 in code:
                print(f"[=] {label} ({type_name}.{method_name}) already patched, skipping.")
                already_patched_count += 1
                continue
            print(f"[!] Couldn't find the {label} Play() call in {type_name}.{method_name}.")
            print("    This build likely changed how the sound is triggered; needs a fresh look.")
            sys.exit(2)
        start, end = hit
        file_off = code_start + start
        length = end - start
        already = all(b == NOP for b in pe.__data__[file_off:file_off + length])
        patches.append((label, type_name, method_name, file_off, length, already))

    already_patched_count += sum(1 for p in patches if p[5])
    if already_patched_count == len(targets):
        print("[=] Already patched. Nothing to do.")
        pe.close()
        return

    pe.close()

    backup_path = dll_path.with_suffix(dll_path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(dll_path, backup_path)
        print(f"[i] Backup written to {backup_path}")
    else:
        print(f"[i] Backup already exists at {backup_path} (not overwritten)")

    data = bytearray(dll_path.read_bytes())
    for label, type_name, method_name, file_off, length, already in patches:
        if already:
            print(f"[=] {label} ({type_name}.{method_name}) already patched, skipping.")
            continue
        print(f"[i] {label}: NOPing {length} bytes at file offset {file_off:#x} in {type_name}.{method_name}")
        data[file_off:file_off + length] = bytes([NOP]) * length

    dll_path.write_bytes(data)
    print(f"[+] Patched {dll_path}")


if __name__ == "__main__":
    main()
