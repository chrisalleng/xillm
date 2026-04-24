#!/usr/bin/env python3
"""
extract_collision.py

Extracts MZB collision geometry from FFXI DAT files and writes
data/collision/<zone_id>.json for use by the mapper Lua addon.

Usage:
    python3 extract_collision.py --ffxi-path "/path/to/FFXI" --zone 107
    python3 extract_collision.py --ffxi-path "/path/to/FFXI" --all

Coordinate system output:
    All vertices are written in *true* Ashita LocalPosition space — the
    same (x, y, z) the game returns at runtime for the player via
    GetPlayerEntity().Movement.LocalPosition. Confirmed empirically: the
    engine's `!pos X Y Z` places the player at Ashita (X, Z, Y), so:
        x = east-west   (= engine.X)
        y = north-south (= engine.Z; "north" in FFXI is -Y)
        z = elevation   (= engine.Y; positive up)

    MZB internal convention has Y sign-flipped vs engine.Y (MZB.Y =
    -engine.Y). The grid-mesh parser applies a row-1 negation inside the
    matrix multiply to cancel this; instance geometry and JSON emission
    then write coords as (vx, tz, vy) / (wx, wz, -wy) so the final values
    are in true Ashita with *no* residual sign flips.

    Anything downstream (navserver, mapper addon, zone_transitions.json,
    obstacles/*.json) should also be in true Ashita. See
    ~/.claude/skills/ffxi-coordinates for the full convention rules.

References:
    https://github.com/LandSandBoat/FFXI-NavMesh-Builder
"""

import argparse
import json
import os
import struct
import sys

# ---------------------------------------------------------------------------
# KeyTable for MZB XOR decode (from KeyTables.cs in FFXI-NavMesh-Builder)
# ---------------------------------------------------------------------------
KEY_TABLE = bytes([
    0xE2, 0xE5, 0x06, 0xA9, 0xED, 0x26, 0xF4, 0x42,
    0x15, 0xF4, 0x81, 0x7F, 0xDE, 0x9A, 0xDE, 0xD0,
    0x1A, 0x98, 0x20, 0x91, 0x39, 0x49, 0x48, 0xA4,
    0x0A, 0x9F, 0x40, 0x69, 0xEC, 0xBD, 0x81, 0x81,
    0x8D, 0xAD, 0x10, 0xB8, 0xC1, 0x88, 0x15, 0x05,
    0x11, 0xB1, 0xAA, 0xF0, 0x0F, 0x1E, 0x34, 0xE6,
    0x81, 0xAA, 0xCD, 0xAC, 0x02, 0x84, 0x33, 0x0A,
    0x19, 0x38, 0x9E, 0xE6, 0x73, 0x4A, 0x11, 0x5D,
    0xBF, 0x85, 0x77, 0x08, 0xCD, 0xD9, 0x96, 0x0D,
    0x79, 0x78, 0xCC, 0x35, 0x06, 0x8E, 0xF9, 0xFE,
    0x66, 0xB9, 0x21, 0x03, 0x20, 0x29, 0x1E, 0x27,
    0xCA, 0x86, 0x82, 0xE6, 0x45, 0x07, 0xDD, 0xA9,
    0xB6, 0xD5, 0xA2, 0x03, 0xEC, 0xAD, 0x62, 0x45,
    0x2D, 0xCE, 0x79, 0xBD, 0x8F, 0x2D, 0x10, 0x18,
    0xE6, 0x0A, 0x6F, 0xAA, 0x6F, 0x46, 0x84, 0x32,
    0x9F, 0x29, 0x2C, 0xC2, 0xF0, 0xEB, 0x18, 0x6F,
    0xF2, 0x3A, 0xDC, 0xEA, 0x7B, 0x0C, 0x81, 0x2D,
    0xCC, 0xEB, 0xA1, 0x51, 0x77, 0x2C, 0xFB, 0x49,
    0xE8, 0x90, 0xF7, 0x90, 0xCE, 0x5C, 0x01, 0xF3,
    0x5C, 0xF4, 0x41, 0xAB, 0x04, 0xE7, 0x16, 0xCC,
    0x3A, 0x05, 0x54, 0x55, 0xDC, 0xED, 0xA4, 0xD6,
    0xBF, 0x3F, 0x9E, 0x08, 0x93, 0xB5, 0x63, 0x38,
    0x90, 0xF7, 0x5A, 0xF0, 0xA2, 0x5F, 0x56, 0xC8,
    0x08, 0x70, 0xCB, 0x24, 0x16, 0xDD, 0xD2, 0x74,
    0x95, 0x3A, 0x1A, 0x2A, 0x74, 0xC4, 0x9D, 0xEB,
    0xAF, 0x69, 0xAA, 0x51, 0x39, 0x65, 0x94, 0xA2,
    0x4B, 0x1F, 0x1A, 0x60, 0x52, 0x39, 0xE8, 0x23,
    0xEE, 0x58, 0x39, 0x06, 0x3D, 0x22, 0x6A, 0x2D,
    0xD2, 0x91, 0x25, 0xA5, 0x2E, 0x71, 0x62, 0xA5,
    0x0B, 0xC1, 0xE5, 0x6E, 0x43, 0x49, 0x7C, 0x58,
    0x46, 0x19, 0x9F, 0x45, 0x49, 0xC6, 0x40, 0x09,
    0xA2, 0x99, 0x5B, 0x7B, 0x98, 0x7F, 0xA0, 0xD0,
])

RESOURCE_TYPE_MZB = 0x1C
RESOURCE_TYPE_MMB = 0x2E

# Second key table for the MMB phase-2 block-swap decryption. Distinct from
# KEY_TABLE (which is shared with MZB). Copied verbatim from
# KeyTables.cs in the FFXI Navmesh Builder reference.
KEY_TABLE2 = bytes([
    0xB8, 0xC5, 0xF7, 0x84, 0xE4, 0x5A, 0x23, 0x7B,
    0xC8, 0x90, 0x1D, 0xF6, 0x5D, 0x09, 0x51, 0xC1,
    0x07, 0x24, 0xEF, 0x5B, 0x1D, 0x73, 0x90, 0x08,
    0xA5, 0x70, 0x1C, 0x22, 0x5F, 0x6B, 0xEB, 0xB0,
    0x06, 0xC7, 0x2A, 0x3A, 0xD2, 0x66, 0x81, 0xDB,
    0x41, 0x62, 0xF2, 0x97, 0x17, 0xFE, 0x05, 0xEF,
    0xA3, 0xDC, 0x22, 0xB3, 0x45, 0x70, 0x3E, 0x18,
    0x2D, 0xB4, 0xBA, 0x0A, 0x65, 0x1D, 0x87, 0xC3,
    0x12, 0xCE, 0x8F, 0x9D, 0xF7, 0x0D, 0x50, 0x24,
    0x3A, 0xF3, 0xCA, 0x70, 0x6B, 0x67, 0x9C, 0xB2,
    0xC2, 0x4D, 0x6A, 0x0C, 0xA8, 0xFA, 0x81, 0xA6,
    0x79, 0xEB, 0xBE, 0xFE, 0x89, 0xB7, 0xAC, 0x7F,
    0x65, 0x43, 0xEC, 0x56, 0x5B, 0x35, 0xDA, 0x81,
    0x3C, 0xAB, 0x6D, 0x28, 0x60, 0x2C, 0x5F, 0x31,
    0xEB, 0xDF, 0x8E, 0x0F, 0x4F, 0xFA, 0xA3, 0xDA,
    0x12, 0x7E, 0xF1, 0xA5, 0xD2, 0x22, 0xA0, 0x0C,
    0x86, 0x8C, 0x0A, 0x0C, 0x06, 0xC7, 0x65, 0x18,
    0xCE, 0xF2, 0xA3, 0x68, 0xFE, 0x35, 0x96, 0x95,
    0xA6, 0xFA, 0x58, 0x63, 0x41, 0x59, 0xEA, 0xDD,
    0x7F, 0xD3, 0x1B, 0xA8, 0x48, 0x44, 0xAB, 0x91,
    0xFD, 0x13, 0xB1, 0x68, 0x01, 0xAC, 0x3A, 0x11,
    0x78, 0x30, 0x33, 0xD8, 0x4E, 0x6A, 0x89, 0x05,
    0x7B, 0x06, 0x8E, 0xB0, 0x86, 0xFD, 0x9F, 0xD7,
    0x48, 0x54, 0x04, 0xAE, 0xF3, 0x06, 0x17, 0x36,
    0x53, 0x3F, 0xA8, 0x11, 0x53, 0xCA, 0xA1, 0x95,
    0xC2, 0xCD, 0xE6, 0x1F, 0x57, 0xB4, 0x7F, 0xAA,
    0xF3, 0x6B, 0xF9, 0xA0, 0x27, 0xD0, 0x09, 0xEF,
    0xF6, 0x68, 0x73, 0x60, 0xDC, 0x50, 0x2A, 0x25,
    0x0F, 0x77, 0xB9, 0xB0, 0x04, 0x0B, 0xE1, 0xCC,
    0x35, 0x31, 0x84, 0xE6, 0x22, 0xF9, 0xC2, 0xAB,
    0x95, 0x91, 0x61, 0xD9, 0x2B, 0xB9, 0x72, 0x4E,
    0x10, 0x76, 0x31, 0x66, 0x0A, 0x0B, 0x2E, 0x83,
])


# ---------------------------------------------------------------------------
# VTABLE/FTABLE file lookup
# ---------------------------------------------------------------------------

def get_rom_path(ffxi_path, zone_id):
    """Return relative path like 'ROM/0/124.DAT' for the zone's terrain file.

    Expansion content (Rise of the Zilart, Chains of Promathia, Treasures of
    Aht Urhgan, Wings of the Goddess, Seekers of Adoulin, etc.) lives in
    ROM2..ROM9, each with its own VTABLE/FTABLE pair. The base VTABLE.DAT
    has a 0 entry for files that moved into an expansion, so we need to try
    each table in turn until we find a non-zero vt."""
    # File ID for zone terrain = zoneId + 100 (for zones 0-255)
    # For zones 256-1299: zoneId + 83635; 1300-1299: zoneId + 66911
    # Reference: d_ms.cs line "var fileId = x < 256 ? x + 100 : x + 83635"
    if zone_id < 256:
        file_id = zone_id + 100
    elif 1000 <= zone_id <= 1299:
        file_id = zone_id + 66911
    else:
        file_id = zone_id + 83635

    # Try the base VTABLE first, then each expansion VTABLE in order.
    tables = [('VTABLE.DAT', 'FTABLE.DAT')]
    for n in range(2, 10):
        tables.append((f'ROM{n}/VTABLE{n}.DAT', f'ROM{n}/FTABLE{n}.DAT'))

    last_err = None
    for vt_rel, ft_rel in tables:
        vtable_path = os.path.join(ffxi_path, vt_rel)
        ftable_path = os.path.join(ffxi_path, ft_rel)
        if not os.path.exists(vtable_path) or not os.path.exists(ftable_path):
            continue
        with open(vtable_path, 'rb') as f:
            vtable = f.read()
        with open(ftable_path, 'rb') as f:
            ftable = f.read()
        if file_id >= len(vtable) or file_id * 2 + 1 >= len(ftable):
            continue
        vt = vtable[file_id]
        ft = struct.unpack_from('<H', ftable, file_id * 2)[0]
        if vt == 0:
            continue
        rom = 'ROM' if vt == 1 else f'ROM{vt}'
        candidate = f'{rom}/{ft >> 7}/{ft & 0x7F}.DAT'
        if os.path.exists(os.path.join(ffxi_path, candidate)):
            return candidate
        last_err = f"{vt_rel} pointed at {candidate} but file missing"

    raise ValueError(
        f"Zone {zone_id} (fileId={file_id}): no VTABLE entry found "
        f"(checked base + ROM2-ROM9). {last_err or ''}"
    )


# ---------------------------------------------------------------------------
# DAT container chunk parsing
# ---------------------------------------------------------------------------

def iter_chunks(data):
    """
    Yield (type_id, block_bytes) for each chunk in a DAT container.

    DAT chunk header layout (16 bytes):
        [0..3]  : chunk name (4 bytes, ASCII)
        [4..7]  : packed uint32: type = bits 0-6, size_field = bits 7-25
        [8..15] : 8 reserved bytes
    data block follows the 16-byte header; length = 16 * size_field - 16
    """
    pos = 0
    while pos + 16 <= len(data):
        value = struct.unpack_from('<I', data, pos + 4)[0]
        type_id = value & 0x7F
        size_field = (value >> 7) & 0x7FFFF
        size = 16 * size_field - 16
        if size < 0 or pos + 16 + size > len(data):
            break
        block = data[pos + 16 : pos + 16 + size]
        yield type_id, block
        pos += 16 + size


# ---------------------------------------------------------------------------
# MZB decode (XOR cipher)
# ---------------------------------------------------------------------------

def decode_mzb(data: bytearray) -> None:
    """Decode an MZB block in-place using the FFXI XOR cipher."""
    if len(data) < 8:
        return
    if data[3] < 0x1B:
        return

    decode_length = struct.unpack_from('<I', data, 0)[0] & 0x00FFFFFF
    decode_length = min(decode_length, len(data))

    key = KEY_TABLE[data[7] ^ 0xFF]
    key_count = 0

    pos = 8
    while pos < decode_length:
        xor_length = ((key >> 4) & 7) + 16
        if (key & 1) == 1 and (pos + xor_length < decode_length):
            for i in range(xor_length):
                data[pos + i] ^= 0xFF
        key = (key + key_count + 1) & 0xFF
        key_count += 1
        pos += xor_length

    # Node header XOR
    node_count = struct.unpack_from('<I', data, 4)[0] & 0x00FFFFFF
    for i in range(node_count):
        for j in range(16):
            idx = 0x20 + i * 0x64 + j
            if idx < len(data):
                data[idx] ^= 0x55


# ---------------------------------------------------------------------------
# MZB geometry extraction
# ---------------------------------------------------------------------------

def _read_i32(data, offset):
    if offset + 4 > len(data):
        return 0
    return struct.unpack_from('<i', data, offset)[0]

def _read_u16(data, offset):
    if offset + 2 > len(data):
        return 0
    return struct.unpack_from('<H', data, offset)[0]

def _read_f32(data, offset):
    if offset + 4 > len(data):
        return 0.0
    return struct.unpack_from('<f', data, offset)[0]

def _read_i16(data, offset):
    if offset + 2 > len(data):
        return 0
    return struct.unpack_from('<h', data, offset)[0]


def _parse_grid_mesh(data, visentryoffset, geometryoffset, all_vertices, all_triangles):
    """
    Extract geometry from one MZB grid mesh cell.

    visentryoffset : byte offset in 'data' to the 4x4 column-major float matrix
    geometryoffset : byte offset in 'data' to the geometry descriptor
    """
    if (visentryoffset <= 0 or visentryoffset >= len(data) or
            geometryoffset <= 0 or geometryoffset >= len(data)):
        return

    # Read 16 floats = 4x4 column-major matrix
    m = [_read_f32(data, visentryoffset + i * 4) for i in range(16)]

    # Geometry descriptor
    vertices_off  = _read_i32(data, geometryoffset + 0x00)
    normals_off   = _read_i32(data, geometryoffset + 0x04)
    tris_off      = _read_i32(data, geometryoffset + 0x08)
    tri_count     = _read_i16(data, geometryoffset + 0x0C)
    flags         = _read_i16(data, geometryoffset + 0x0E)

    if tri_count <= 0 or vertices_off <= 0 or tris_off <= 0:
        return

    # Number of vertices = (normalsOffset - verticesOffset) / 12
    # (each vertex is 3 × float32 = 12 bytes)
    num_vert = (normals_off - vertices_off) // 12 if normals_off > vertices_off else 0
    if num_vert <= 0:
        return

    # Rotation matrix (3×3, column-major from the 4×4)
    # m[col*4 + row]  →  m[0]=M11, m[1]=M12, m[2]=M13, m[4]=M21 ...
    # Transformation used in ParseGridMesh:
    #   X = m[0]*lx + m[4]*ly + m[8]*lz + m[12]
    #   Y = -(m[1]*lx + m[5]*ly + m[9]*lz + m[13])   [negated!]
    #   Z = m[2]*lx + m[6]*ly + m[10]*lz + m[14]
    # WriteObj outputs: v X Y -Z  (Z negated again)
    # So final coord: (X, Y, -Z) matches Ashita LocalPosition
    m2 = [m[0], m[1], m[2],
          m[4], m[5], m[6],
          m[8], m[9], m[10]]
    determ = (m2[0] * (m2[4] * m2[8] - m2[5] * m2[7])
            - m2[1] * (m2[3] * m2[8] - m2[5] * m2[6])
            + m2[2] * (m2[3] * m2[7] - m2[4] * m2[6]))

    base_vert = len(all_vertices)

    # Transform and collect vertices
    for i in range(num_vert):
        if vertices_off <= 0:
            continue
        lx = _read_f32(data, vertices_off + (i * 3 + 0) * 4)
        ly = _read_f32(data, vertices_off + (i * 3 + 1) * 4)
        lz = _read_f32(data, vertices_off + (i * 3 + 2) * 4)
        w  = 1.0

        # Filter: skip vertices far below terrain (same check as NavMesh-Builder)
        tz = m[2] * lx + m[6] * ly + m[10] * lz + m[14] * w
        if tz <= -99329:
            all_vertices.append(None)  # placeholder to keep indexing consistent
            continue

        vx = m[0] * lx + m[4] * ly + m[8]  * lz + m[12] * w
        vy = -(m[1] * lx + m[5] * ly + m[9]  * lz + m[13] * w)
        # vz = tz = m[2]*lx + m[6]*ly + m[10]*lz + m[14] (already computed)

        # JSON vertex storage convention:
        #   V.x = engine.X        (matches runtime Ashita.X, no flip)
        #   V.y = -engine.Z       (SIGN-FLIPPED vs runtime Ashita.Y)
        #   V.z = engine.Y        (matches runtime Ashita.Z = elevation)
        # The Y flip is cancelled by load_collision (Recast.Z = -V.y) plus
        # the server's _load_transitions (t['y'] = -t['y']) plus
        # game_to_recast (Recast.Z = Ashita.y). Net: Recast sees consistent
        # coords for terrain, transitions, and player.
        all_vertices.append((vx, -tz, vy))

    # Collect triangle indices
    # Each triangle entry is 4 × uint16: [iv2, iv1, iv0, in0] or [iv0, iv1, iv2, in0]
    # depending on determinant sign (winding order)
    for i in range(tri_count):
        base = tris_off + i * 4 * 2
        i0 = _read_u16(data, base + 0 * 2) & 0x3FFF
        i1 = _read_u16(data, base + 1 * 2) & 0x3FFF
        i2 = _read_u16(data, base + 2 * 2) & 0x3FFF
        # Note: in0 (normal index) at offset 3 is not needed for collision

        if determ > 0:
            # Reversed winding
            gi0 = base_vert + i2
            gi1 = base_vert + i1
            gi2 = base_vert + i0
        else:
            gi0 = base_vert + i0
            gi1 = base_vert + i1
            gi2 = base_vert + i2

        # Only emit triangle if all three vertices are valid (not None)
        if (gi0 < len(all_vertices) and gi1 < len(all_vertices) and gi2 < len(all_vertices)
                and all_vertices[gi0] is not None
                and all_vertices[gi1] is not None
                and all_vertices[gi2] is not None):
            all_triangles.append((gi0, gi1, gi2))


def _parse_grid_entry(data, entry_offs, x, y, all_vertices, all_triangles):
    if entry_offs <= 0 or entry_offs >= len(data):
        return

    entries = []
    offset = entry_offs
    while offset + 4 <= len(data):
        c = _read_i32(data, offset)
        if c == 0:
            break
        entries.append(c)
        offset += 4

    if not entries:
        return

    # entries[0] = packed position/flags
    # entries[1..] = pairs of (visentryoffset, geometryoffset)
    i = 1
    while i + 1 < len(entries):
        vis_off  = entries[i]
        geom_off = entries[i + 1]
        if vis_off > 0 and geom_off > 0:
            _parse_grid_mesh(data, vis_off, geom_off, all_vertices, all_triangles)
        i += 2


def parse_mzb(data):
    """
    Parse a decoded MZB block.
    Returns (vertices, triangles) where:
        vertices  = list of (x, y, z) in Ashita LocalPosition space
        triangles = list of (i0, i1, i2) integer index triples
    """
    data = bytearray(data)  # ensure mutable copy for decode
    decode_mzb(data)

    all_vertices  = []
    all_triangles = []

    # Find meshoffset: first non-zero int32 starting at offset 8
    m_offset = 8
    mesh_offset = 0
    while m_offset + 4 <= len(data):
        mesh_offset = _read_i32(data, m_offset)
        if mesh_offset != 0:
            break
        m_offset += 4

    if mesh_offset <= 0 or mesh_offset >= len(data):
        return all_vertices, all_triangles

    # Grid section
    grid_offset = _read_i32(data, mesh_offset + 0x10)
    if grid_offset > 0 and 0x0C < len(data) and 0x0D < len(data):
        grid_width  = data[0x0C] * 10
        grid_height = data[0x0D] * 10

        for gy in range(grid_height * 10):
            for gx in range(grid_width * 10):
                idx = (gy * grid_width * 10 + gx) * 4
                if grid_offset + idx + 4 > len(data):
                    continue
                entry_offset = _read_i32(data, grid_offset + idx)
                if entry_offset > 0 and entry_offset < len(data):
                    _parse_grid_entry(data, entry_offset, gx, gy,
                                      all_vertices, all_triangles)

    return all_vertices, all_triangles


# ---------------------------------------------------------------------------
# MMB decryption + geometry parsing
# ---------------------------------------------------------------------------

def decode_mmb(data: bytearray) -> None:
    """Decrypt an MMB block in-place. Two-phase XOR + block-swap, keyed off
    data[5] ^ 0xF0. See MMB.cs in LandSandBoat/FFXI-NavMesh-Builder."""
    if len(data) < 8 or data[3] < 5:
        return
    n = len(data)
    # Phase 1: rotating XOR keyed off KEY_TABLE[data[5] ^ 0xF0].
    key = KEY_TABLE[data[5] ^ 0xF0]
    keyCount = 0
    for pos in range(8, n):
        x = ((key & 0xFF) << 8) | (key & 0xFF)
        keyCount += 1
        key += keyCount
        data[pos] ^= (x >> (key & 7)) & 0xFF
        keyCount += 1
        key += keyCount
    # Phase 2: conditional block-swap on 8-byte pairs, gated on data[6..7] == 0xFFFF.
    if data[6] != 0xFF or data[7] != 0xFF:
        return
    key1 = data[5] ^ 0xF0
    key2 = KEY_TABLE2[key1]
    len2 = ((n - 8) & ~0xF) >> 1
    off1 = 8
    off2 = off1 + len2
    for _ in range(0, len2, 8):
        if (key2 & 1) == 1:
            tmp = bytes(data[off1:off1 + 8])
            data[off1:off1 + 8] = data[off2:off2 + 8]
            data[off2:off2 + 8] = tmp
        key1 += 9
        key2 = (key2 + key1) & 0xFF
        off1 += 8
        off2 += 8


def parse_mmb(data: bytes):
    """Parse a decoded MMB chunk. Returns dict or None on failure:
      { 'imgID': str, 'models': [ {'vertices': [(x,y,z), ...],
                                   'indices': [(i0,i1,i2), ...]} ] }
    References: SMMBHEAD/SMMBHEAD2, SMMBHeader, SMMBBlockHeader, SMMBModelHeader,
    SMMBBlockVertex[2] in FFXILandscapeMesh.h."""
    try:
        if len(data) < 56:
            return None

        id_bytes = data[0:3]
        if id_bytes == b'MMB':
            raw = struct.unpack_from('<I', data, 4)[0]
            mmb_type = 1
            head_type = raw & 0x7F
            use_vertex2 = False
        else:
            mmb_type = 2
            d3 = data[4]
            use_vertex2 = (d3 == 2)
            head_type = 0
        pos = 16

        img_id = data[pos:pos + 16].decode('ascii', errors='replace').rstrip(' ').rstrip('\x00')
        pos += 16
        pieces = struct.unpack_from('<i', data, pos)[0]
        pos += 4
        pos += 24  # bbox: 6 floats
        offset_block_header = struct.unpack_from('<I', data, pos)[0]
        pos += 4

        piece_offsets = []
        if offset_block_header == 0:
            if pieces != 0:
                for _ in range(8):
                    if pos + 4 > len(data):
                        break
                    off = struct.unpack_from('<I', data, pos)[0]
                    pos += 4
                    if off != 0:
                        piece_offsets.append(off)
            else:
                piece_offsets.append(pos)
        else:
            piece_offsets.append(offset_block_header)
            max_range = offset_block_header - pos
            if max_range > 0:
                for _ in range(max_range // 4):
                    if pos + 4 > len(data):
                        break
                    off = struct.unpack_from('<I', data, pos)[0]
                    pos += 4
                    if off != 0:
                        piece_offsets.append(off)
            pos = offset_block_header

        all_models = []
        for _ in range(pieces):
            if piece_offsets:
                pos = piece_offsets.pop(0)
            if pos + 36 > len(data):
                break

            num_model = struct.unpack_from('<i', data, pos)[0]
            pos += 4
            pos += 24  # piece bbox
            pos += 4   # numFace

            if num_model < 0 or num_model > 50:
                break

            for _ in range(num_model):
                if pos + 20 > len(data):
                    break
                pos += 16  # textureName
                vertex_size = struct.unpack_from('<H', data, pos)[0]
                pos += 2
                pos += 2  # blending

                stride = 48 if use_vertex2 else 36
                if pos + vertex_size * stride > len(data):
                    break
                vertices = []
                for i in range(vertex_size):
                    x, y, z = struct.unpack_from('<3f', data, pos)
                    vertices.append((x, y, z))
                    pos += stride

                if pos + 4 > len(data):
                    break
                num_indices = struct.unpack_from('<I', data, pos)[0] & 0xFFFF
                pos += 4

                indices = []
                # Triangle list if MMB1 + type==0 or MMB2 + d3==2; else strip.
                use_tri_list = (mmb_type == 1 and head_type == 0) or (mmb_type == 2 and use_vertex2)
                if pos + num_indices * 2 > len(data):
                    break
                if use_tri_list:
                    num_tri = num_indices // 3
                    for _t in range(num_tri):
                        i1 = struct.unpack_from('<H', data, pos)[0] & 0x3FFF
                        pos += 2
                        i2 = struct.unpack_from('<H', data, pos)[0] & 0x3FFF
                        pos += 2
                        i3 = struct.unpack_from('<H', data, pos)[0] & 0x3FFF
                        pos += 2
                        if i1 < vertex_size and i2 < vertex_size and i3 < vertex_size:
                            indices.append((i1, i2, i3))
                    remaining = num_indices - num_tri * 3
                    pos += remaining * 2
                else:
                    strip = []
                    for _i in range(num_indices):
                        idx = struct.unpack_from('<H', data, pos)[0] & 0x3FFF
                        pos += 2
                        strip.append(idx)
                    for i in range(2, len(strip)):
                        a, b, c = strip[i - 2], strip[i - 1], strip[i]
                        if a == b or b == c or a == c:
                            continue
                        if a >= vertex_size or b >= vertex_size or c >= vertex_size:
                            continue
                        if i % 2 == 0:
                            indices.append((a, b, c))
                        else:
                            indices.append((b, a, c))

                if num_indices % 2 == 1:
                    pos += 2

                all_models.append({'vertices': vertices, 'indices': indices})

        return {'imgID': img_id, 'models': all_models}
    except (struct.error, IndexError, ValueError):
        return None


def parse_mzb_instances(mzb_data: bytearray):
    """Extract SMZBBlock100 records (instance transforms + MMB refs).

    Each node is 100 bytes starting at 0x20; first 16 bytes were XOR-decoded
    in decode_mzb. Fields: id[16], pos(3f), rot(3f), scale(3f), LOD(4f), 8 ints.

    Also extracts the collision flag: byte[1] of the first trailing int (at
    node offset 0x45). Observed values:
      0x02 — walk-through (grass, vegetation, decoration; no player collision)
      0x00 — solid (stones, walls, buildings, invisible hitwalls)
      0x01 — special/interactive (rare; treated as solid for safety)
    """
    node_count = struct.unpack_from('<I', mzb_data, 4)[0] & 0x00FFFFFF
    instances = []
    for i in range(node_count):
        offset = 0x20 + i * 0x64
        if offset + 100 > len(mzb_data):
            break
        name = bytes(mzb_data[offset:offset + 16]).decode('ascii', errors='replace').rstrip(' ').rstrip('\x00')
        if not name:
            continue
        try:
            tx, ty, tz = struct.unpack_from('<3f', mzb_data, offset + 16)
            rx, ry, rz = struct.unpack_from('<3f', mzb_data, offset + 28)
            sx, sy, sz = struct.unpack_from('<3f', mzb_data, offset + 40)
        except struct.error:
            continue
        vals = (tx, ty, tz, rx, ry, rz, sx, sy, sz)
        if any(v != v or v == float('inf') or v == -float('inf') for v in vals):
            continue
        collision_flag = mzb_data[offset + 0x45]
        instances.append({
            'name': name,
            'pos': (tx, ty, tz),
            'rot': (rx, ry, rz),
            'scale': (sx, sy, sz),
            'collision': collision_flag,
        })
    return instances


def instance_matrix(inst):
    """Build the 4x4 MZB→world transform for an instance record.
    Rotation is intrinsic XYZ (applied X then Y then Z), mirroring the
    getMZB100Matrix() reference in FFXILandscapeMesh.cpp."""
    rx, ry, rz = inst['rot']
    sx, sy, sz = inst['scale']
    tx, ty, tz = inst['pos']
    import math
    cx, sxr = math.cos(rx), math.sin(rx)
    cy, syr = math.cos(ry), math.sin(ry)
    cz, szr = math.cos(rz), math.sin(rz)
    # Column-major storage (matches the reference):
    col0 = (cy * cz,                cy * szr,                 -syr)
    col1 = (cz * sxr * syr - cx * szr,  cx * cz + sxr * syr * szr,  cy * sxr)
    col2 = (cx * cz * syr + sxr * szr,  -cz * sxr + cx * syr * szr, cx * cy)
    # Apply scale to each column
    col0 = (col0[0] * sx, col0[1] * sx, col0[2] * sx)
    col1 = (col1[0] * sy, col1[1] * sy, col1[2] * sy)
    col2 = (col2[0] * sz, col2[1] * sz, col2[2] * sz)
    # Return as row-major 3x4 tuple: (m00, m01, m02, m03, m10, ...)
    return (
        col0[0], col1[0], col2[0], tx,
        col0[1], col1[1], col2[1], ty,
        col0[2], col1[2], col2[2], tz,
    )


def apply_instance_transform(verts, m):
    """Transform MMB local vertices by the MZB instance matrix, emit in the
    same storage convention as parse_mzb: (engine.X, -engine.Z, engine.Y).
    See parse_mzb for why Y is sign-flipped in storage."""
    m00, m01, m02, m03, m10, m11, m12, m13, m20, m21, m22, m23 = m
    out = []
    for x, y, z in verts:
        wx = m00 * x + m01 * y + m02 * z + m03
        wy = m10 * x + m11 * y + m12 * z + m13
        wz = m20 * x + m21 * y + m22 * z + m23
        out.append((wx, -wz, -wy))
    return out


# ---------------------------------------------------------------------------
# Build JSON output
# ---------------------------------------------------------------------------

WALKABLE_NORMAL_Z = 0.707


def _triangle_signed_normal_z(v0, v1, v2):
    """Return the normalised Z-component of the face normal (positive = normal
    points in +Z of the caller's coord system). Used for both the walkability
    filter and winding normalization so Recast sees consistent CCW-from-above
    winding."""
    ex1 = v1[0] - v0[0]; ey1 = v1[1] - v0[1]; ez1 = v1[2] - v0[2]
    ex2 = v2[0] - v0[0]; ey2 = v2[1] - v0[1]; ez2 = v2[2] - v0[2]
    nx = ey1 * ez2 - ez1 * ey2
    ny = ez1 * ex2 - ex1 * ez2
    nz = ex1 * ey2 - ey1 * ex2
    length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if length < 1e-10:
        return 0.0
    return nz / length


def _triangle_abs_normal_z(v0, v1, v2):
    return abs(_triangle_signed_normal_z(v0, v1, v2))


def build_collision_json(zone_id, vertices, triangle_indices, terrain_count=None):
    """
    Build the collision JSON dict expected by geometry_provider.lua.

    Format uses indexed vertices to minimise file size:
    {
        "zone_id": <int>,
        "metadata": { "source": "MZB", "vertex_count": N, "triangle_count": N },
        "bounds": { "min": [x,y,z], "max": [x,y,z] },
        "vertices": [[x,y,z], ...],    -- all unique verts (2 dp)
        "triangles": [[i0,i1,i2], ...]  -- index triples into vertices
    }

    The first `terrain_count` entries of `triangle_indices` are MZB terrain and
    get the walkability filter (keep |nz| >= 0.707). Any entries past that
    boundary are instance walls (already pre-filtered to keep only the
    vertical faces) and pass through unchanged. When `terrain_count` is None
    the filter applies to every triangle (back-compat for terrain-only extracts).
    """
    # Remap only the vertices actually used and de-duplicate via rounding.
    # Key = (rx, ry, rz) at 2 decimal places; value = compacted index.
    vert_map = {}
    compact_verts = []
    compact_tris  = []
    if terrain_count is None:
        terrain_count = len(triangle_indices)

    for tri_i, (i0, i1, i2) in enumerate(triangle_indices):
        # Validate raw indices
        if (i0 >= len(vertices) or i1 >= len(vertices) or i2 >= len(vertices) or
                vertices[i0] is None or vertices[i1] is None or vertices[i2] is None):
            continue

        v0, v1, v2 = vertices[i0], vertices[i1], vertices[i2]

        if tri_i < terrain_count:
            # Walkable-only filter. Additionally, ~half of MZB terrain
            # triangles come out with the WRONG winding: in storage coords
            # (V.z = -runtime.Z), a correctly-wound walkable floor has
            # signed normal.z NEGATIVE (because storage flips runtime +Z up).
            # Triangles with positive storage normal.z are the inverted half;
            # without fixing them, Recast sees them as ceilings after
            # load_collision's winding swap and the navmesh ends up with
            # holes in otherwise-walkable terrain.
            snz = _triangle_signed_normal_z(v0, v1, v2)
            if abs(snz) < WALKABLE_NORMAL_Z:
                continue
            if snz > 0:
                i1, i2 = i2, i1
                v1, v2 = v2, v1

        tri_idxs = []
        for vx, vy, vz in (v0, v1, v2):
            key = (round(vx, 2), round(vy, 2), round(vz, 2))
            if key not in vert_map:
                vert_map[key] = len(compact_verts)
                compact_verts.append(list(key))
            tri_idxs.append(vert_map[key])
        compact_tris.append(tri_idxs)

    if compact_verts:
        xs = [v[0] for v in compact_verts]
        ys = [v[1] for v in compact_verts]
        zs = [v[2] for v in compact_verts]
        bounds = {
            "min": [min(xs), min(ys), min(zs)],
            "max": [max(xs), max(ys), max(zs)],
        }
    else:
        bounds = {"min": [0, 0, 0], "max": [0, 0, 0]}

    return {
        "zone_id": zone_id,
        "metadata": {
            "source": "MZB",
            "vertex_count": len(compact_verts),
            "triangle_count": len(compact_tris),
        },
        "bounds": bounds,
        "vertices":  compact_verts,
        "triangles": compact_tris,
    }


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract_zone(ffxi_path, zone_id, output_dir, debug_edge_names=None):
    # Instance geometry is ALWAYS included. The extractor does not support a
    # terrain-only mode. Cross-zone collision must always reflect the MMB
    # objects (rocks, hitwalls, fences, ruins) the player physically bumps
    # into. Any path-failure issues are to be fixed elsewhere, not by
    # dropping instances.
    if debug_edge_names is None:
        debug_edge_names = set()
    print(f"Zone {zone_id}: looking up ROM path ...", end=" ", flush=True)
    rel_path = get_rom_path(ffxi_path, zone_id)
    dat_path = os.path.join(ffxi_path, rel_path.replace('\\', '/'))
    if not os.path.exists(dat_path):
        print(f"NOT FOUND ({dat_path})")
        return False

    file_size = os.path.getsize(dat_path)
    print(f"{rel_path} ({file_size // 1024} KB)")

    with open(dat_path, 'rb') as f:
        dat_data = f.read()

    # Find MZB chunk + collect MMB chunks
    mzb_block = None
    mmb_blocks = []
    for type_id, block in iter_chunks(dat_data):
        if type_id == RESOURCE_TYPE_MZB and mzb_block is None:
            mzb_block = block
        elif type_id == RESOURCE_TYPE_MMB:
            mmb_blocks.append(block)

    if mzb_block is None:
        print(f"  ERROR: No MZB chunk found in {rel_path}")
        return False

    print(f"  MZB chunk: {len(mzb_block)} bytes, MMB chunks: {len(mmb_blocks)}")

    vertices, triangle_indices = parse_mzb(mzb_block)
    terrain_tris = len(triangle_indices)
    print(f"  Terrain: {len(vertices)} vertices, {terrain_tris} triangles")

    # Parse MMB model meshes (keyed by imgID)
    mmb_models = {}
    for raw in mmb_blocks:
        buf = bytearray(raw)
        decode_mmb(buf)
        parsed = parse_mmb(bytes(buf))
        if parsed is not None:
            mmb_models[parsed['imgID']] = parsed

    # Parse MZB instance records (SMZBBlock100). Apply only instances that have
    # real player collision (collision flag != 0x02). 0x02 is the "walk-through"
    # flag used for grass, hedges, and small decorations — excluding them keeps
    # the navmesh from being fragmented by thousands of sub-1y clutter items.
    matched = 0
    inst_tris = 0
    inst_verts_added = 0
    missing_models = set()
    instance_records = []
    mzb_mut = bytearray(mzb_block)
    decode_mzb(mzb_mut)
    all_instances = parse_mzb_instances(mzb_mut)
    solid_inst = [i for i in all_instances if i['collision'] != 0x02]
    for inst in solid_inst:
        model = mmb_models.get(inst['name'])
        if model is None:
            missing_models.add(inst['name'])
            continue
        matched += 1
        m = instance_matrix(inst)
        for sub_idx, sub in enumerate(model['models']):
            if not sub['vertices'] or not sub['indices']:
                continue
            world_verts = apply_instance_transform(sub['vertices'], m)
            base = len(vertices)
            for v in world_verts:
                vertices.append(v)
            inst_verts_added += len(world_verts)
            # Only emit wall + slope triangles, skip the horizontal faces.
            # The horizontal tops of a stone/wall would otherwise be marked as
            # walkable by Recast, creating a navmesh-walkable platform on top of
            # the obstacle instead of treating the obstacle as solid. Keeping
            # only the steeper triangles forces Recast to see the object as a
            # wall that blocks path generation at ground level.
            wall_vert_idxs = set()
            wall_tri_local = []
            for a, b, c in sub['indices']:
                va = world_verts[a]
                vb = world_verts[b]
                vc = world_verts[c]
                # Face normal Z component (JSON coord Z = elevation-negated)
                ex1 = vb[0] - va[0]; ey1 = vb[1] - va[1]; ez1 = vb[2] - va[2]
                ex2 = vc[0] - va[0]; ey2 = vc[1] - va[1]; ez2 = vc[2] - va[2]
                nz = ex1 * ey2 - ey1 * ex2
                nl_sq = (ey1 * ez2 - ez1 * ey2) ** 2 + (ez1 * ex2 - ex1 * ez2) ** 2 + nz * nz
                if nl_sq < 1e-20:
                    continue
                nl = nl_sq ** 0.5
                abs_nz = abs(nz) / nl
                if abs_nz > 0.707:
                    # Nearly horizontal — would become walkable; skip it so
                    # Recast doesn't generate a poly on the object's top face.
                    continue
                triangle_indices.append((base + a, base + b, base + c))
                inst_tris += 1
                wall_vert_idxs.add(a); wall_vert_idxs.add(b); wall_vert_idxs.add(c)
                wall_tri_local.append((a, b, c))

            # Emit a bbox per submesh, built ONLY from verts that participate
            # in emitted wall triangles. This is exactly the geometry Recast
            # sees as collision — skipping the unused "top face" verts that
            # would otherwise inflate the bbox. See
            # ~/.claude/skills/ffxi-coordinates for the storage → runtime
            # Ashita axis flips (negate both Y and Z).
            if wall_vert_idxs:
                wv_idxs_sorted = sorted(wall_vert_idxs)
                wv = [world_verts[i] for i in wv_idxs_sorted]
                xs = [v[0] for v in wv]
                ys_true = [-v[1] for v in wv]
                zs_true = [-v[2] for v in wv]
                label = inst['name'] if len(model['models']) == 1 else f"{inst['name']}[{sub_idx}]"
                record = {
                    'name': label,
                    'collision': inst['collision'],
                    'bbox_min': [round(min(xs), 2), round(min(ys_true), 2), round(min(zs_true), 2)],
                    'bbox_max': [round(max(xs), 2), round(max(ys_true), 2), round(max(zs_true), 2)],
                }
                # Optionally emit full wall-triangle mesh for the addon to draw
                # line outlines. Indexed format: verts[] + tris[[i0,i1,i2]...],
                # all in runtime Ashita (Y and Z negated from storage).
                if inst['name'] in debug_edge_names:
                    remap = {old: new for new, old in enumerate(wv_idxs_sorted)}
                    record['verts'] = [[round(xs[i], 2),
                                        round(ys_true[i], 2),
                                        round(zs_true[i], 2)]
                                       for i in range(len(wv))]
                    record['tris'] = [[remap[a], remap[b], remap[c]]
                                      for a, b, c in wall_tri_local
                                      if a in remap and b in remap and c in remap]
                instance_records.append(record)
    walk_through = len(all_instances) - len(solid_inst)
    print(f"  Instances: {len(all_instances)} total, {walk_through} walk-through skipped, "
          f"{matched}/{len(solid_inst)} solid matched to MMB "
          f"(+{inst_verts_added} verts, +{inst_tris} tris)")
    if missing_models:
        print(f"  Missing MMB for {len(missing_models)} model names: {sorted(missing_models)[:5]}")

    if not triangle_indices:
        print("  WARNING: No triangles extracted")

    out = build_collision_json(zone_id, vertices, triangle_indices, terrain_count=terrain_tris)
    out['metadata']['terrain_triangle_count'] = terrain_tris
    out['metadata']['instance_triangle_count'] = inst_tris
    out['metadata']['solid_instance_count'] = matched
    if instance_records:
        out['instances'] = instance_records

    out_path = os.path.join(output_dir, f"{zone_id}.json")
    with open(out_path, 'w') as f:
        json.dump(out, f, separators=(',', ':'))

    # Also emit a thin per-zone instance file for the Ashita mapper addon to
    # draw object bounding boxes. Small enough to parse in-game; the full
    # collision JSON is too big for that.
    if instance_records:
        inst_dir = os.path.join(os.path.dirname(output_dir), 'instances')
        os.makedirs(inst_dir, exist_ok=True)
        inst_path = os.path.join(inst_dir, f"{zone_id}.json")
        with open(inst_path, 'w') as f:
            json.dump({'zone_id': zone_id, 'instances': instance_records},
                      f, separators=(',', ':'))

    size_kb = os.path.getsize(out_path) // 1024
    print(f"  Wrote {out_path} ({size_kb} KB, {out['metadata']['triangle_count']} triangles)")
    if out['bounds']['min'] != [0,0,0]:
        b = out['bounds']
        print(f"  Bounds X: [{b['min'][0]:.1f}, {b['max'][0]:.1f}]  "
              f"Y: [{b['min'][1]:.1f}, {b['max'][1]:.1f}]  "
              f"Z: [{b['min'][2]:.1f}, {b['max'][2]:.1f}]")
    return True


# ---------------------------------------------------------------------------
# All-zones zone ID range (standard FFXI zones 1-255 + expansion zones)
# ---------------------------------------------------------------------------

ZONE_RANGE_STANDARD = list(range(1, 256))


def main():
    parser = argparse.ArgumentParser(description="Extract FFXI MZB collision data to JSON")
    parser.add_argument('--ffxi-path', required=True,
                        help='Path to FFXI installation directory (contains VTABLE.DAT)')
    parser.add_argument('--zone', type=int, default=None,
                        help='Single zone ID to extract (e.g. 107 for South Gustaberg)')
    parser.add_argument('--all', action='store_true',
                        help='Extract all standard zones (1-255)')
    parser.add_argument('--output', default=None,
                        help='Output directory (default: ../../data/collision/ relative to script)')
    parser.add_argument('--debug-edges', default='',
                        help='Comma-separated MMB model names (e.g. pl_moa_st1_m). For '
                             'matching instances, emit wall-triangle mesh (verts+tris) in '
                             'the instances JSON so the addon can draw line outlines of '
                             'the actual collision shape instead of just a bbox wireframe.')
    args = parser.parse_args()
    debug_edge_names = {n.strip() for n in args.debug_edges.split(',') if n.strip()}

    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_out = os.path.normpath(os.path.join(script_dir, '../../data/collision'))
    output_dir = args.output or default_out
    os.makedirs(output_dir, exist_ok=True)

    ffxi_path = os.path.normpath(args.ffxi_path)

    if args.zone is not None:
        extract_zone(ffxi_path, args.zone, output_dir,
                     debug_edge_names=debug_edge_names)
    elif args.all:
        ok = 0
        fail = 0
        for z in ZONE_RANGE_STANDARD:
            try:
                if extract_zone(ffxi_path, z, output_dir,
                                debug_edge_names=debug_edge_names):
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                print(f"  Zone {z}: exception: {e}")
                fail += 1
        print(f"\nDone: {ok} succeeded, {fail} failed/skipped")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
