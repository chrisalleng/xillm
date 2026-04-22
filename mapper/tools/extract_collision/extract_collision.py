#!/usr/bin/env python3
"""
extract_collision.py

Extracts MZB collision geometry from FFXI DAT files and writes
data/collision/<zone_id>.json for use by the mapper Lua addon.

Usage:
    python3 extract_collision.py --ffxi-path "/path/to/FFXI" --zone 107
    python3 extract_collision.py --ffxi-path "/path/to/FFXI" --all

Coordinate system output:
    All vertices are written in Ashita LocalPosition space:
        x = east-west   (Ashita LocalPosition.X)
        y = north-south (Ashita LocalPosition.Y, large horizontal axis, = -MZB.Z)
        z = elevation   (Ashita LocalPosition.Z, small vertical axis, = -MZB.Y)

    Derivation: MZB grid-mesh vertices are stored after applying a 4x4
    column-major matrix, with row-1 (Y) pre-negated and the final OBJ
    output negates Z again.  The resulting (X, Y, -Z) triple matches the
    Ashita game coordinate system observed in-engine.

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


# ---------------------------------------------------------------------------
# VTABLE/FTABLE file lookup
# ---------------------------------------------------------------------------

def get_rom_path(ffxi_path, zone_id):
    """Return relative path like 'ROM/0/124.DAT' for the zone's terrain file."""
    # File ID for zone terrain = zoneId + 100 (for zones 0-255)
    # For zones 256-1299: zoneId + 83635; 1300-1299: zoneId + 66911
    # Reference: d_ms.cs line "var fileId = x < 256 ? x + 100 : x + 83635"
    if zone_id < 256:
        file_id = zone_id + 100
    elif 1000 <= zone_id <= 1299:
        file_id = zone_id + 66911
    else:
        file_id = zone_id + 83635

    vtable_path = os.path.join(ffxi_path, 'VTABLE.DAT')
    ftable_path = os.path.join(ffxi_path, 'FTABLE.DAT')

    if not os.path.exists(vtable_path) or not os.path.exists(ftable_path):
        raise FileNotFoundError(f"VTABLE.DAT or FTABLE.DAT not found in {ffxi_path}")

    with open(vtable_path, 'rb') as f:
        vtable = f.read()
    with open(ftable_path, 'rb') as f:
        ftable = f.read()

    if file_id >= len(vtable):
        raise ValueError(f"file_id {file_id} out of range for VTABLE ({len(vtable)} bytes)")

    vt = vtable[file_id]
    ft = struct.unpack_from('<H', ftable, file_id * 2)[0]

    if vt == 0:
        raise ValueError(
            f"Zone {zone_id} (fileId={file_id}): VTABLE entry is 0, "
            "file not found in base ROM tables"
        )
    if vt == 1:
        return f'ROM/{ft >> 7}/{ft & 0x7F}.DAT'
    else:
        return f'ROM{vt}/{ft >> 7}/{ft & 0x7F}.DAT'


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

        # Coordinate mapping to Ashita LocalPosition space:
        #   Ashita.X = V.X = vx  (east-west, large horizontal extent)
        #   Ashita.Y = -V.Z = -tz  (north-south, large horizontal extent)
        #   Ashita.Z = V.Y = vy  (elevation, small vertical range)
        # Derivation: Ashita.Y = -MZB.Z, Ashita.Z = -MZB.Y where
        # MZB.Z = V.Z (stored Z before WriteObj negate) and
        # MZB.Y = -V.Y (pre-negation form, so -MZB.Y = V.Y = elevation).
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
# Build JSON output
# ---------------------------------------------------------------------------

def _triangle_normal_z(v0, v1, v2):
    """
    Return the Z component of the face normal for a triangle in Ashita space.
    In the output coordinate system:
        x = east-west  (LocalPosition.X)
        y = north-south (LocalPosition.Y)
        z = elevation   (LocalPosition.Z)  ← up direction
    A walkable surface has a positive upward (Z) normal component.
    """
    ex1 = v1[0] - v0[0]; ey1 = v1[1] - v0[1]; ez1 = v1[2] - v0[2]
    ex2 = v2[0] - v0[0]; ey2 = v2[1] - v0[1]; ez2 = v2[2] - v0[2]
    # Cross product: normal = e1 × e2
    nx = ey1 * ez2 - ez1 * ey2
    ny = ez1 * ex2 - ex1 * ez2
    nz = ex1 * ey2 - ey1 * ex2
    length = (nx*nx + ny*ny + nz*nz) ** 0.5
    if length < 1e-10:
        return 0.0
    return abs(nz) / length  # abs: accept both winding orders


# Minimum upward normal component for a walkable surface (cos 45° ≈ 0.707).
WALKABLE_NORMAL_Z = 0.707


def build_collision_json(zone_id, vertices, triangle_indices):
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

    geometry_provider.lua assembles per-triangle vertex data at load time.
    """
    # Remap only the vertices actually used and de-duplicate via rounding.
    # Key = (rx, ry, rz) at 2 decimal places; value = compacted index.
    # Only walkable triangles (face-normal Y ≥ WALKABLE_NORMAL_Y) are kept.
    vert_map = {}
    compact_verts = []
    compact_tris  = []

    for i0, i1, i2 in triangle_indices:
        # Validate raw indices
        if (i0 >= len(vertices) or i1 >= len(vertices) or i2 >= len(vertices) or
                vertices[i0] is None or vertices[i1] is None or vertices[i2] is None):
            continue

        v0, v1, v2 = vertices[i0], vertices[i1], vertices[i2]

        # Walkability filter: skip steep / vertical / ceiling surfaces
        if _triangle_normal_z(v0, v1, v2) < WALKABLE_NORMAL_Z:
            continue

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

def extract_zone(ffxi_path, zone_id, output_dir):
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

    # Find MZB chunk
    mzb_block = None
    for type_id, block in iter_chunks(dat_data):
        if type_id == RESOURCE_TYPE_MZB:
            mzb_block = block
            break

    if mzb_block is None:
        print(f"  ERROR: No MZB chunk found in {rel_path}")
        return False

    print(f"  MZB chunk: {len(mzb_block)} bytes")

    vertices, triangle_indices = parse_mzb(mzb_block)
    print(f"  Extracted: {len(vertices)} vertices, {len(triangle_indices)} triangles")

    if not triangle_indices:
        print("  WARNING: No triangles extracted")

    out = build_collision_json(zone_id, vertices, triangle_indices)

    out_path = os.path.join(output_dir, f"{zone_id}.json")
    with open(out_path, 'w') as f:
        json.dump(out, f, separators=(',', ':'))

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
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_out = os.path.normpath(os.path.join(script_dir, '../../data/collision'))
    output_dir = args.output or default_out
    os.makedirs(output_dir, exist_ok=True)

    ffxi_path = os.path.normpath(args.ffxi_path)

    if args.zone is not None:
        extract_zone(ffxi_path, args.zone, output_dir)
    elif args.all:
        ok = 0
        fail = 0
        for z in ZONE_RANGE_STANDARD:
            try:
                if extract_zone(ffxi_path, z, output_dir):
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
