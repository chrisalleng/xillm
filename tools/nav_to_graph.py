#!/usr/bin/env python3
"""
nav_to_graph.py  —  Convert Recast/Detour .nav files to mapper zone_<id>.json graphs.

Usage:
    python3 nav_to_graph.py <navmesh_dir> <output_dir>

    navmesh_dir  : directory containing .nav files from LandSandBoat/xiNavmeshes
    output_dir   : directory to write zone_<id>.json files (e.g. Ashita config/addons/mapper/)

Each output file matches the existing zone_<id>.json format that mapper.lua already loads:
    { "zone_id": N, "nodes": [...], "edges": [...] }

Coordinate conversion (navmesh → Ashita LocalPosition):
    LocalPosition.X =  navmesh[0]   east/west  (unchanged)
    LocalPosition.Y = -navmesh[2]   north/south (negated navmesh Z)
    LocalPosition.Z = -navmesh[1]   elevation   (negated navmesh Y)
"""

import struct
import json
import math
import os
import sys
import re
from collections import defaultdict

# ---------------------------------------------------------------------------
# Recast/Detour binary constants
# ---------------------------------------------------------------------------
NAVMESHSET_MAGIC   = 0x4D534554   # 'MSET' LE
NAVMESH_MAGIC      = 0x444E4156   # 'DNAV' LE  (tile magic)
NAVMESHSET_VERSION = 1
NAVMESH_VERSION    = 7
DT_VERTS_PER_POLY  = 6
POLY_SIZE          = 4 + DT_VERTS_PER_POLY * 2 + DT_VERTS_PER_POLY * 2 + 2 + 1 + 1  # 32 bytes
LINK_SIZE          = 12   # ref(4) + next(4) + edge(1) + side(1) + bmin(1) + bmax(1)
DT_NULL_LINK       = 0xFFFFFFFF
DT_EXT_LINK        = 0x8000       # set in neis[] to indicate a cross-tile link

# Graph tuning
MIN_NODE_DIST = 1.0   # yalms — minimum spacing between kept nodes after density filter
MAX_NODES     = 8000  # if a zone exceeds this after 1.0-yalm filter, coarsen adaptively

# ---------------------------------------------------------------------------
# Zone name → zone ID mapping  (LandSandBoat zone IDs)
# ---------------------------------------------------------------------------
ZONE_MAP = {
    # Zone IDs from LandSandBoat zone_settings.sql (authoritative source).
    # Keys are navmesh filename stems (without .nav).
    # Numbered stubs left in the repo that rename.py didn't rename:
    "49":  49,    # zone 49 = "none" (no name in DB)
    "133": 133,   # zone 133 = Outer_RaKaznar_[U2]

    # Full zone list
    "Phanauet_Channel":               1,
    "Carpenters_Landing":             2,
    "Manaclipper":                    3,
    "Bibiki_Bay":                     4,
    "Uleguerand_Range":               5,
    "Bearclaw_Pinnacle":              6,
    "Attohwa_Chasm":                  7,
    "Boneyard_Gully":                 8,
    "PsoXja":                         9,
    "The_Shrouded_Maw":               10,
    "Oldton_Movalpolos":              11,
    "Newton_Movalpolos":              12,
    "Mine_Shaft_2716":                13,
    "Hall_of_Transference":           14,
    "Abyssea-Konschtat":              15,
    "Promyvion-Holla":                16,
    "Spire_of_Holla":                 17,
    "Promyvion-Dem":                  18,
    "Spire_of_Dem":                   19,
    "Promyvion-Mea":                  20,
    "Spire_of_Mea":                   21,
    "Promyvion-Vahzl":                22,
    "Spire_of_Vahzl":                 23,
    "Lufaise_Meadows":                24,
    "Misareaux_Coast":                25,
    "Tavnazian_Safehold":             26,
    "Phomiuna_Aqueducts":             27,
    "Sacrarium":                      28,
    "Riverne-Site_B01":               29,
    "Riverne-Site_A01":               30,
    "Monarch_Linn":                   31,
    "Sealions_Den":                   32,
    "AlTaieu":                        33,
    "Grand_Palace_of_HuXzoi":         34,
    "The_Garden_of_RuHmet":           35,
    "Empyreal_Paradox":               36,
    "Temenos":                        37,
    "Apollyon":                       38,
    "Dynamis-Valkurm":                39,
    "Dynamis-Buburimu":               40,
    "Dynamis-Qufim":                  41,
    "Dynamis-Tavnazia":               42,
    "Diorama_Abdhaljs-Ghelsba":       43,
    "Abdhaljs_Isle-Purgonorgo":       44,
    "Abyssea-Tahrongi":               45,
    "Open_sea_route_to_Al_Zahbi":     46,
    "Open_sea_route_to_Mhaura":       47,
    "Al_Zahbi":                       48,
    "Aht_Urhgan_Whitegate":           50,
    "Wajaom_Woodlands":               51,
    "Bhaflau_Thickets":               52,
    "Nashmau":                        53,
    "Arrapago_Reef":                  54,
    "Ilrusi_Atoll":                   55,
    "Periqia":                        56,
    "Talacca_Cove":                   57,
    "Silver_Sea_route_to_Nashmau":    58,
    "Silver_Sea_route_to_Al_Zahbi":   59,
    "The_Ashu_Talif":                 60,
    "Mount_Zhayolm":                  61,
    "Halvung":                        62,
    "Lebros_Cavern":                  63,
    "Navukgo_Execution_Chamber":      64,
    "Mamook":                         65,
    "Mamool_Ja_Training_Grounds":     66,
    "Jade_Sepulcher":                 67,
    "Aydeewa_Subterrane":             68,
    "Leujaoam_Sanctum":               69,
    "Chocobo_Circuit":                70,
    "The_Colosseum":                  71,
    "Alzadaal_Undersea_Ruins":        72,
    "Zhayolm_Remnants":               73,
    "Arrapago_Remnants":              74,
    "Bhaflau_Remnants":               75,
    "Bhaflau_ Remnants":              75,   # typo variant in repo
    "Silver_Sea_Remnants":            76,
    "Nyzul_Isle":                     77,
    "Hazhalm_Testing_Grounds":        78,
    "Caedarva_Mire":                  79,
    "Southern_San_dOria_[S]":         80,
    "East_Ronfaure_[S]":              81,
    "Jugner_Forest_[S]":              82,
    "Vunkerl_Inlet_[S]":              83,
    "Batallia_Downs_[S]":             84,
    "La_Vaule_[S]":                   85,
    "Everbloom_Hollow":               86,
    "Bastok_Markets_[S]":             87,
    "North_Gustaberg_[S]":            88,
    "Grauberg_[S]":                   89,
    "Pashhow_Marshlands_[S]":         90,
    "Rolanberry_Fields_[S]":          91,
    "Beadeaux_[S]":                   92,
    "Ruhotz_Silvermines":             93,
    "Windurst_Waters_[S]":            94,
    "West_Sarutabaruta_[S]":          95,
    "Fort_Karugo-Narugo_[S]":         96,
    "Meriphataud_Mountains_[S]":      97,
    "Sauromugue_Champaign_[S]":       98,
    "Castle_Oztroja_[S]":             99,
    "West_Ronfaure":                  100,
    "East_Ronfaure":                  101,
    "La_Theine_Plateau":              102,
    "Valkurm_Dunes":                  103,
    "Jugner_Forest":                  104,
    "Batallia_Downs":                 105,
    "North_Gustaberg":                106,
    "South_Gustaberg":                107,
    "Konschtat_Highlands":            108,
    "Pashhow_Marshlands":             109,
    "Rolanberry_Fields":              110,
    "Beaucedine_Glacier":             111,
    "Xarcabard":                      112,
    "Cape_Teriggan":                  113,
    "Eastern_Altepa_Desert":          114,
    "West_Sarutabaruta":              115,
    "East_Sarutabaruta":              116,
    "Tahrongi_Canyon":                117,
    "Buburimu_Peninsula":             118,
    "Meriphataud_Mountains":          119,
    "Sauromugue_Champaign":           120,
    "The_Sanctuary_of_ZiTah":         121,
    "RoMaeve":                        122,
    "Yuhtunga_Jungle":                123,
    "Yhoator_Jungle":                 124,
    "Western_Altepa_Desert":          125,
    "Qufim_Island":                   126,
    "Behemoths_Dominion":             127,
    "Valley_of_Sorrows":              128,
    "Ghoyus_Reverie":                 129,
    "RuAun_Gardens":                  130,
    "Mordion_Gaol":                   131,
    "Abyssea-La_Theine":              132,
    "Outer_RaKaznar_[U2]":            133,
    "Dynamis-Beaucedine":             134,
    "Dynamis-Xarcabard":              135,
    "Beaucedine_Glacier_[S]":         136,
    "Xarcabard_[S]":                  137,
    "Castle_Zvahl_Baileys_[S]":       138,
    "Horlais_Peak":                   139,
    "Ghelsba_Outpost":                140,
    "Fort_Ghelsba":                   141,
    "Yughott_Grotto":                 142,
    "Palborough_Mines":               143,
    "Waughroon_Shrine":               144,
    "Giddeus":                        145,
    "Balgas_Dais":                    146,
    "Beadeaux":                       147,
    "Qulun_Dome":                     148,
    "Davoi":                          149,
    "Monastic_Cavern":                150,
    "Castle_Oztroja":                 151,
    "Altar_Room":                     152,
    "The_Boyahda_Tree":               153,
    "Dragons_Aery":                   154,
    "Castle_Zvahl_Keep_[S]":          155,
    "Throne_Room_[S]":                156,
    "Middle_Delkfutts_Tower":         157,
    "Upper_Delkfutts_Tower":          158,
    "Temple_of_Uggalepih":            159,
    "Den_of_Rancor":                  160,
    "Castle_Zvahl_Baileys":           161,
    "Castle_Zvahl_Keep":              162,
    "Sacrificial_Chamber":            163,
    "Garlaige_Citadel_[S]":           164,
    "Throne_Room":                    165,
    "Ranguemont_Pass":                166,
    "Bostaunieux_Oubliette":          167,
    "Chamber_of_Oracles":             168,
    "Toraimarai_Canal":               169,
    "Full_Moon_Fountain":             170,
    "Crawlers_Nest_[S]":              171,
    "Zeruhn_Mines":                   172,
    "Korroloka_Tunnel":               173,
    "Kuftal_Tunnel":                  174,
    "The_Eldieme_Necropolis_[S]":     175,
    "Sea_Serpent_Grotto":             176,
    "VeLugannon_Palace":              177,
    "The_Shrine_of_RuAvitau":         178,
    "Stellar_Fulcrum":                179,
    "LaLoff_Amphitheater":            180,
    "The_Celestial_Nexus":            181,
    "Walk_of_Echoes":                 182,
    "Maquette_Abdhaljs-Legion_A":     183,
    "Lower_Delkfutts_Tower":          184,
    "Dynamis-San_dOria":              185,
    "Dynamis-Bastok":                 186,
    "Dynamis-Windurst":               187,
    "Dynamis-Jeuno":                  188,
    "Outer_RaKaznar_[U3]":            189,
    "King_Ranperres_Tomb":            190,
    "Dangruf_Wadi":                   191,
    "Inner_Horutoto_Ruins":           192,
    "Ordelles_Caves":                 193,
    "Outer_Horutoto_Ruins":           194,
    "The_Eldieme_Necropolis":         195,
    "Gusgen_Mines":                   196,
    "Crawlers_Nest":                  197,
    "Maze_of_Shakhrami":              198,
    "Residential_Area":               199,
    "Garlaige_Citadel":               200,
    "Cloister_of_Gales":              201,
    "Cloister_of_Storms":             202,
    "Cloister_of_Frost":              203,
    "FeiYin":                         204,
    "Ifrits_Cauldron":                205,
    "QuBia_Arena":                    206,
    "Cloister_of_Flames":             207,
    "Quicksand_Caves":                208,
    "Cloister_of_Tremors":            209,
    "GM_Home":                        210,
    "Cloister_of_Tides":              211,
    "Gustav_Tunnel":                  212,
    "Labyrinth_of_Onzozo":            213,
    "Abyssea-Attohwa":                215,
    "Abyssea-Misareaux":              216,
    "Abyssea-Vunkerl":                217,
    "Abyssea-Altepa":                 218,
    "Ship_bound_for_Selbina":         220,
    "Ship_bound_for_Mhaura":          221,
    "Provenance":                     222,
    "San_dOria-Jeuno_Airship":        223,
    "Bastok-Jeuno_Airship":           224,
    "Windurst-Jeuno_Airship":         225,
    "Kazham-Jeuno_Airship":           226,
    "Ship_bound_for_Selbina_Pirates": 227,
    "Ship_bound_for_Mhaura_Pirates":  228,
    "Throne_Room_[V]":                229,
    "Southern_San_dOria":             230,
    "Northern_San_dOria":             231,
    "Port_San_dOria":                 232,
    "Chateau_dOraguille":             233,
    "Bastok_Mines":                   234,
    "Bastok_Markets":                 235,
    "Port_Bastok":                    236,
    "Metalworks":                     237,
    "Windurst_Waters":                238,
    "Windurst_Walls":                 239,
    "Port_Windurst":                  240,
    "Windurst_Woods":                 241,
    "Heavens_Tower":                  242,
    "RuLude_Gardens":                 243,
    "Upper_Jeuno":                    244,
    "Lower_Jeuno":                    245,
    "Port_Jeuno":                     246,
    "Rabao":                          247,
    "Selbina":                        248,
    "Mhaura":                         249,
    "Kazham":                         250,
    "Hall_of_the_Gods":               251,
    "Norg":                           252,
    "Abyssea-Uleguerand":             253,
    "Abyssea-Grauberg":               254,
    "Abyssea-Empyreal_Paradox":       255,
    "Western_Adoulin":                256,
    "Eastern_Adoulin":                257,
    "Rala_Waterways":                 258,
    "Rala_Waterways_U":               259,
    "Yahse_Hunting_Grounds":          260,
    "Ceizak_Battlegrounds":           261,
    "Foret_de_Hennetiel":             262,
    "Yorcia_Weald":                   263,
    "Yorcia_Weald_U":                 264,
    "Morimar_Basalt_Fields":          265,
    "Marjami_Ravine":                 266,
    "Kamihr_Drifts":                  267,
    "Sih_Gates":                      268,
    "Moh_Gates":                      269,
    "Cirdas_Caverns":                 270,
    "Cirdas_Caverns_U":               271,
    "Dho_Gates":                      272,
    "Woh_Gates":                      273,
    "Outer_RaKaznar":                 274,
    "Outer_RaKaznar_U":               275,
    "Outer_RaKaznar_[U1]":            275,
    "RaKaznar_Inner_Court":           276,
    "RaKaznar_Turris":                277,
    "Walk_of_Echoes_[P2]":            279,
    "Mog_Garden":                     280,
    "Leafallia":                      281,
    "Mount_Kamihr":                   282,
    "Celennia_Memorial_Library":      284,
    "Feretory":                       285,
    "Maquette_Abdhaljs-Legion_B":     287,
    "Escha_ZiTah":                    288,
    "Escha-ZiTah":                    288,   # hyphen variant in repo
    "Escha_RuAun":                    289,
    "Desuetia_Empyreal_Paradox":      290,
    "Reisenjima":                     291,
    "Reisenjima_Henge":               292,
    "Reisenjima_Sanctorium":          293,
    "Dynamis-San_dOria_[D]":          294,
    "Dynamis-Bastok_[D]":             295,
    "Dynamis-Windurst_[D]":           296,
    "Dynamis-Jeuno_[D]":              297,
    "Walk_of_Echoes_[P1]":            298,
}

# ---------------------------------------------------------------------------
# Coordinate conversion
# ---------------------------------------------------------------------------
def navmesh_to_local(nx, ny, nz):
    """Convert Recast navmesh (x,y,z) to Ashita LocalPosition (X,Y,Z).

    Navmesh: x=east/west, y=elevation, z=north/south
    Ashita:  X=east/west, Y=north/south, Z=elevation

    LandSandBoat ToDetourPos negates both y and z, so we negate them back:
        local_x =  nx
        local_y = -nz   (north/south)
        local_z = -ny   (elevation)
    """
    return nx, -nz, -ny

# ---------------------------------------------------------------------------
# Binary parsing helpers
# ---------------------------------------------------------------------------
def u32(data, offset): return struct.unpack_from('<I', data, offset)[0]
def i32(data, offset): return struct.unpack_from('<i', data, offset)[0]
def f32(data, offset): return struct.unpack_from('<f', data, offset)[0]
def u16(data, offset): return struct.unpack_from('<H', data, offset)[0]
def u8 (data, offset): return struct.unpack_from('<B', data, offset)[0]

def read_vec3(data, offset):
    return struct.unpack_from('<3f', data, offset)

# ---------------------------------------------------------------------------
# Parse a single tile blob
# ---------------------------------------------------------------------------
def parse_tile_blob(blob, dt_poly_bits, dt_tile_bits):
    o = 0

    # dtMeshHeader (100 bytes)
    magic      = i32(blob, o); o += 4
    version    = i32(blob, o); o += 4
    tx         = i32(blob, o); o += 4
    ty         = i32(blob, o); o += 4
    layer      = i32(blob, o); o += 4
    userId     = u32(blob, o); o += 4
    polyCount  = i32(blob, o); o += 4
    vertCount  = i32(blob, o); o += 4
    maxLinkCount = i32(blob, o); o += 4
    detailMeshCount = i32(blob, o); o += 4
    detailVertCount = i32(blob, o); o += 4
    detailTriCount  = i32(blob, o); o += 4
    bvNodeCount     = i32(blob, o); o += 4
    offMeshConCount = i32(blob, o); o += 4
    offMeshBase     = i32(blob, o); o += 4
    o += 12  # walkableHeight, walkableRadius, walkableClimb (floats)
    o += 24  # bmin[3], bmax[3] (floats)
    o += 4   # bvQuantFactor (float)
    assert o == 100, f"dtMeshHeader size mismatch: {o}"

    # Vertices
    verts = []
    for i in range(vertCount):
        verts.append(read_vec3(blob, o)); o += 12

    # Polygons
    polys = []
    for i in range(polyCount):
        po = o + i * POLY_SIZE
        firstLink  = u32(blob, po)
        vert_refs  = struct.unpack_from('<6H', blob, po + 4)
        neis       = struct.unpack_from('<6H', blob, po + 16)
        flags      = u16(blob, po + 28)
        vc         = u8 (blob, po + 30)
        area       = u8 (blob, po + 31)
        polys.append({
            'firstLink': firstLink,
            'verts':     list(vert_refs[:vc]),
            'neis':      list(neis[:vc]),
            'vc':        vc,
            'flags':     flags,
            'area':      area,
        })
    o += polyCount * POLY_SIZE

    # Links
    links = []
    for i in range(maxLinkCount):
        lo = o + i * LINK_SIZE
        links.append({
            'ref':  u32(blob, lo),
            'next': u32(blob, lo + 4),
            'edge': u8 (blob, lo + 8),
            'side': u8 (blob, lo + 9),
        })
    o += maxLinkCount * LINK_SIZE

    return {
        'verts':        verts,
        'polys':        polys,
        'links':        links,
        'offMeshBase':  offMeshBase,
        'polyCount':    polyCount,
    }

# ---------------------------------------------------------------------------
# Parse a complete .nav file
# ---------------------------------------------------------------------------
def parse_nav_file(filepath):
    with open(filepath, 'rb') as f:
        data = f.read()

    offset = 0

    # File header
    magic, version, numTiles = struct.unpack_from('<iii', data, offset); offset += 12
    if magic != NAVMESHSET_MAGIC:
        raise ValueError(f"Bad navmesh magic: 0x{magic:08x} in {filepath}")

    # dtNavMeshParams
    orig       = read_vec3(data, offset); offset += 12
    tileWidth  = f32(data, offset); offset += 4
    tileHeight = f32(data, offset); offset += 4
    maxTiles   = i32(data, offset); offset += 4
    maxPolys   = i32(data, offset); offset += 4

    dt_poly_bits = max(1, int(math.log2(maxPolys)))  if maxPolys > 1 else 1
    dt_tile_bits = max(1, int(math.log2(maxTiles)))  if maxTiles > 1 else 1

    tiles = []
    tile_idx_to_arr = {}   # tile_idx (from tileRef) → index in tiles[]

    for _ in range(numTiles):
        tileRef  = u32(data, offset); offset += 4
        dataSize = i32(data, offset); offset += 4
        if dataSize <= 0:
            continue
        blob = data[offset:offset + dataSize]; offset += dataSize

        tile_idx = (tileRef >> dt_poly_bits) & ((1 << dt_tile_bits) - 1)
        tile_data = parse_tile_blob(blob, dt_poly_bits, dt_tile_bits)
        tile_data['tileRef'] = tileRef
        tile_data['tile_idx'] = tile_idx

        arr_idx = len(tiles)
        tiles.append(tile_data)
        tile_idx_to_arr[tile_idx] = arr_idx

    return tiles, dt_poly_bits, dt_tile_bits, tile_idx_to_arr

# ---------------------------------------------------------------------------
# Build graph from parsed tiles
# ---------------------------------------------------------------------------
def build_graph(tiles, dt_poly_bits, dt_tile_bits, tile_idx_to_arr):
    """Extract polygon centroids as nodes and build edges from adjacency."""

    # --- Pass 1: assign global node IDs to walkable polygons ---------------
    # poly_node_id[arr_idx][poly_local_idx] = global_node_id  (or -1 if skipped)
    poly_node_id = []
    nodes = []   # list of (lx, ly, lz)

    for arr_idx, tile in enumerate(tiles):
        verts       = tile['verts']
        polys       = tile['polys']
        offMeshBase = tile['offMeshBase']
        tile_map    = {}

        for pi, poly in enumerate(polys):
            # Skip off-mesh connections (they don't have valid floor positions)
            if pi >= offMeshBase:
                tile_map[pi] = -1
                continue
            # Compute centroid from vertex positions in navmesh space
            vc = poly['vc']
            if vc == 0:
                tile_map[pi] = -1
                continue
            cx = sum(verts[v][0] for v in poly['verts']) / vc
            cy = sum(verts[v][1] for v in poly['verts']) / vc
            cz = sum(verts[v][2] for v in poly['verts']) / vc
            lx, ly, lz = navmesh_to_local(cx, cy, cz)
            node_id = len(nodes)
            nodes.append((lx, ly, lz))
            tile_map[pi] = node_id

        poly_node_id.append(tile_map)

    # --- Pass 2: build edges -----------------------------------------------
    # edges_set stores (min_id, max_id) pairs to deduplicate
    edges_set = set()

    def add_edge(a, b):
        if a >= 0 and b >= 0 and a != b:
            edges_set.add((min(a, b), max(a, b)))

    for arr_idx, tile in enumerate(tiles):
        polys = tile['polys']
        links = tile['links']
        tile_map = poly_node_id[arr_idx]

        for pi, poly in enumerate(polys):
            node_a = tile_map.get(pi, -1)
            if node_a < 0:
                continue

            for ei, nei in enumerate(poly['neis']):
                if nei == 0:
                    continue  # no neighbour

                if nei & DT_EXT_LINK:
                    # Cross-tile link: walk the dtLink chain looking for this edge
                    link_idx = poly['firstLink']
                    while link_idx != DT_NULL_LINK:
                        lnk = links[link_idx]
                        if lnk['edge'] == ei:
                            ref = lnk['ref']
                            if ref != 0:
                                linked_poly_idx = ref & ((1 << dt_poly_bits) - 1)
                                linked_tile_idx = (ref >> dt_poly_bits) & ((1 << dt_tile_bits) - 1)
                                linked_arr_idx  = tile_idx_to_arr.get(linked_tile_idx, -1)
                                if linked_arr_idx >= 0:
                                    node_b = poly_node_id[linked_arr_idx].get(linked_poly_idx, -1)
                                    add_edge(node_a, node_b)
                        link_idx = lnk['next']
                else:
                    # Same-tile neighbour: neis[] stores 1-based poly index
                    nb_pi = nei - 1
                    node_b = tile_map.get(nb_pi, -1)
                    add_edge(node_a, node_b)

    return nodes, list(edges_set)

# ---------------------------------------------------------------------------
# Density filter: remove nodes closer than MIN_NODE_DIST to an already-kept node
# ---------------------------------------------------------------------------
def dist3d(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)

def filter_density(nodes, edges, min_dist=MIN_NODE_DIST):
    """Keep a subset of nodes with minimum spacing; remap edges accordingly."""

    # Spatial bucket grid for fast nearest-kept lookup
    bucket_size = min_dist
    buckets = defaultdict(list)
    kept_ids = []          # original node IDs that are kept
    old_to_new = {}        # original id → new id in kept list
    nearest_kept = {}      # original id → nearest kept original id

    def bucket_key(node):
        return (int(node[0] / bucket_size), int(node[1] / bucket_size), int(node[2] / bucket_size))

    def has_nearby_kept(node):
        bk = bucket_key(node)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for kept_orig in buckets.get((bk[0]+dx, bk[1]+dy, bk[2]+dz), []):
                        if dist3d(node, nodes[kept_orig]) < min_dist:
                            return True
        return False

    def nearest_kept_id(node):
        bk = bucket_key(node)
        best_d, best_k = math.inf, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for kept_orig in buckets.get((bk[0]+dx, bk[1]+dy, bk[2]+dz), []):
                        d = dist3d(node, nodes[kept_orig])
                        if d < best_d:
                            best_d, best_k = d, kept_orig
        return best_k

    for i, node in enumerate(nodes):
        if not has_nearby_kept(node):
            new_id = len(kept_ids)
            kept_ids.append(i)
            old_to_new[i] = new_id
            buckets[bucket_key(node)].append(i)
        else:
            nearest_kept[i] = None  # will resolve below

    # For non-kept nodes, find the nearest kept node to redirect edges
    for i, node in enumerate(nodes):
        if i not in old_to_new:
            nearest_kept[i] = nearest_kept_id(node)

    def resolve(old_id):
        if old_id in old_to_new:
            return old_to_new[old_id]
        k = nearest_kept.get(old_id)
        if k is not None and k in old_to_new:
            return old_to_new[k]
        return -1

    # Build new node list
    new_nodes = [nodes[i] for i in kept_ids]

    # Remap edges (drop self-loops and duplicate edges created by merging)
    new_edges_set = set()
    for (a, b) in edges:
        na, nb = resolve(a), resolve(b)
        if na >= 0 and nb >= 0 and na != nb:
            new_edges_set.add((min(na, nb), max(na, nb)))

    return new_nodes, list(new_edges_set)

# ---------------------------------------------------------------------------
# Convert a single .nav file to our JSON graph format
# ---------------------------------------------------------------------------
def convert_nav(filepath, zone_id):
    tiles, dt_poly_bits, dt_tile_bits, tile_idx_to_arr = parse_nav_file(filepath)
    raw_nodes, raw_edges = build_graph(tiles, dt_poly_bits, dt_tile_bits, tile_idx_to_arr)

    # Start at MIN_NODE_DIST (1.0 yalm) for maximum accuracy; coarsen adaptively
    # if the result would exceed MAX_NODES (keeps A* fast on large outdoor zones).
    min_dist = MIN_NODE_DIST
    nodes, edges = filter_density(raw_nodes, raw_edges, min_dist)
    while len(nodes) > MAX_NODES and min_dist < 20.0:
        min_dist *= 1.5
        nodes, edges = filter_density(raw_nodes, raw_edges, min_dist)

    # Build JSON-ready structures (1-based node IDs to match existing mapper format)
    json_nodes = [
        {"id": i + 1, "x": round(n[0], 3), "y": round(n[1], 3), "z": round(n[2], 3)}
        for i, n in enumerate(nodes)
    ]
    json_edges = [
        {"a": a + 1, "b": b + 1}
        for (a, b) in edges
    ]

    return {
        "zone_id": zone_id,
        "nodes":   json_nodes,
        "edges":   json_edges,
    }

# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def main():
    if len(sys.argv) < 3:
        print("Usage: nav_to_graph.py <navmesh_dir> <output_dir>")
        sys.exit(1)

    nav_dir = sys.argv[1]
    out_dir = sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)

    nav_files = [f for f in os.listdir(nav_dir) if f.endswith('.nav')]
    print(f"Found {len(nav_files)} .nav files")

    ok = 0
    skipped = 0
    errors = []

    for filename in sorted(nav_files):
        stem = filename[:-4]  # strip .nav
        zone_id = ZONE_MAP.get(stem)
        if zone_id is None:
            print(f"  SKIP  {filename}  (no zone ID mapping)")
            skipped += 1
            continue

        filepath = os.path.join(nav_dir, filename)
        out_path = os.path.join(out_dir, f"zone_{zone_id}.json")

        try:
            data = convert_nav(filepath, zone_id)
            with open(out_path, 'w') as f:
                json.dump(data, f, separators=(',', ':'))
            print(f"  OK    {filename}  →  zone_{zone_id}.json"
                  f"  ({len(data['nodes'])} nodes, {len(data['edges'])} edges)")
            ok += 1
        except Exception as e:
            print(f"  ERROR {filename}: {e}")
            errors.append((filename, str(e)))

    print(f"\nDone: {ok} converted, {skipped} skipped (no ID), {len(errors)} errors")
    if errors:
        print("Errors:")
        for fn, err in errors:
            print(f"  {fn}: {err}")

if __name__ == '__main__':
    main()
