#!/usr/bin/env python3
"""
gen_zone_transitions.py

Parses LandSandBoat's zone_settings.sql and zonelines.sql to produce
mapper/zone_transitions.lua — a Lua module containing:
  - ZONE_NAMES[id]  = "Display Name"
  - ZONE_IDS["name"] = id  (lowercase, underscores replaced by spaces)
  - EXITS[from_zone] = { {fx, fy, to_zone, tx, ty}, ... }

Coordinate conversion (server → Ashita LocalPosition):
  local_x = pos_x           (east/west, same axis)
  local_y = +pos_z          (north/south: server z has the SAME sign as LocalPosition.Y)
  local_z = -pos_y          (elevation: server y is negated)

Note: nav_to_graph.py uses LocalPosition.Y = -navmesh[2] because the navmesh Z axis
is north-positive (opposite of the server's pos_z which is south-positive).
Both end up in the same LocalPosition.Y space; no sign conflict.
"""

import re
import sys
from collections import defaultdict

ZONE_SETTINGS = 'zone_settings.sql'
ZONELINES     = 'zonelines.sql'
OUTPUT        = '../mapper/zone_transitions.lua'

# ---------------------------------------------------------------------------
# Autotranslate lookup table from LandSandBoat scripts/commands/zone.lua
# Format: (groupId, messageId) -> xi.zone constant name (UPPER_SNAKE_CASE)
# Sourced directly from the zoneList in zone.lua.
# ---------------------------------------------------------------------------
AT_ZONE_LIST = [
    (0x14, 0xA9, 'PHANAUET_CHANNEL'),
    (0x14, 0xAA, 'CARPENTERS_LANDING'),
    (0x14, 0x84, 'MANACLIPPER'),
    (0x14, 0x85, 'BIBIKI_BAY'),
    (0x14, 0x8A, 'ULEGUERAND_RANGE'),
    (0x14, 0x8B, 'BEARCLAW_PINNACLE'),
    (0x14, 0x86, 'ATTOHWA_CHASM'),
    (0x14, 0x87, 'BONEYARD_GULLY'),
    (0x14, 0x88, 'PSOXJA'),
    (0x14, 0x89, 'THE_SHROUDED_MAW'),
    (0x14, 0x8C, 'OLDTON_MOVALPOLOS'),
    (0x14, 0x8D, 'NEWTON_MOVALPOLOS'),
    (0x14, 0x8E, 'MINE_SHAFT_2716'),
    (0x14, 0xAB, 'HALL_OF_TRANSFERENCE'),
    (0x14, 0x9B, 'PROMYVION_HOLLA'),
    (0x14, 0x9C, 'SPIRE_OF_HOLLA'),
    (0x14, 0x9E, 'PROMYVION_DEM'),
    (0x14, 0x9F, 'SPIRE_OF_DEM'),
    (0x14, 0xA0, 'PROMYVION_MEA'),
    (0x14, 0xA2, 'SPIRE_OF_MEA'),
    (0x14, 0xA3, 'PROMYVION_VAHZL'),
    (0x14, 0xA7, 'SPIRE_OF_VAHZL'),
    (0x14, 0x90, 'LUFAISE_MEADOWS'),
    (0x14, 0x91, 'MISAREAUX_COAST'),
    (0x14, 0x8F, 'TAVNAZIAN_SAFEHOLD'),
    (0x14, 0x93, 'PHOMIUNA_AQUEDUCTS'),
    (0x14, 0x94, 'SACRARIUM'),
    (0x14, 0x96, 'RIVERNE_SITE_B01'),
    (0x14, 0x98, 'RIVERNE_SITE_A01'),
    (0x14, 0x99, 'MONARCH_LINN'),
    (0x14, 0x92, 'SEALIONS_DEN'),
    (0x14, 0xAC, 'ALTAIEU'),
    (0x14, 0xAD, 'GRAND_PALACE_OF_HUXZOI'),
    (0x14, 0xAE, 'THE_GARDEN_OF_RUHMET'),
    (0x14, 0xB0, 'EMPYREAL_PARADOX'),
    (0x14, 0xB1, 'TEMENOS'),
    (0x14, 0xB2, 'APOLLYON'),
    (0x14, 0xB4, 'DYNAMIS_VALKURM'),
    (0x14, 0xB5, 'DYNAMIS_BUBURIMU'),
    (0x14, 0xB6, 'DYNAMIS_QUFIM'),
    (0x14, 0xB7, 'DYNAMIS_TAVNAZIA'),
    (0x14, 0xAF, 'DIORAMA_ABDHALJS_GHELSBA'),
    (0x14, 0xB8, 'ABDHALJS_ISLE_PURGONORGO'),
    (0x14, 0xB9, 'OPEN_SEA_ROUTE_TO_AL_ZAHBI'),
    (0x14, 0xBA, 'OPEN_SEA_ROUTE_TO_MHAURA'),
    (0x14, 0xBB, 'AL_ZAHBI'),
    (0x14, 0xDB, 'AHT_URHGAN_WHITEGATE'),
    (0x14, 0xBC, 'AHT_URHGAN_WHITEGATE'),
    (0x14, 0xBD, 'WAJAOM_WOODLANDS'),
    (0x14, 0xBE, 'BHAFLAU_THICKETS'),
    (0x14, 0xBF, 'NASHMAU'),
    (0x14, 0xC0, 'ARRAPAGO_REEF'),
    (0x14, 0xC1, 'ILRUSI_ATOLL'),
    (0x14, 0xC2, 'PERIQIA'),
    (0x14, 0xC3, 'TALACCA_COVE'),
    (0x14, 0xC4, 'SILVER_SEA_ROUTE_TO_NASHMAU'),
    (0x14, 0xC5, 'SILVER_SEA_ROUTE_TO_AL_ZAHBI'),
    (0x14, 0xC6, 'THE_ASHU_TALIF'),
    (0x14, 0xC7, 'MOUNT_ZHAYOLM'),
    (0x14, 0xC8, 'HALVUNG'),
    (0x14, 0xC9, 'LEBROS_CAVERN'),
    (0x14, 0xCA, 'NAVUKGO_EXECUTION_CHAMBER'),
    (0x14, 0xCB, 'MAMOOK'),
    (0x14, 0xCC, 'MAMOOL_JA_TRAINING_GROUNDS'),
    (0x14, 0xCD, 'JADE_SEPULCHER'),
    (0x14, 0xCE, 'AYDEEWA_SUBTERRANE'),
    (0x14, 0xCF, 'LEUJAOAM_SANCTUM'),
    (0x27, 0x0F, 'CHOCOBO_CIRCUIT'),
    (0x27, 0x10, 'THE_COLOSSEUM'),
    (0x14, 0xDD, 'ALZADAAL_UNDERSEA_RUINS'),
    (0x14, 0xDE, 'ZHAYOLM_REMNANTS'),
    (0x14, 0xDF, 'ARRAPAGO_REMNANTS'),
    (0x14, 0xE0, 'BHAFLAU_REMNANTS'),
    (0x14, 0xE1, 'SILVER_SEA_REMNANTS'),
    (0x14, 0xE2, 'NYZUL_ISLE'),
    (0x14, 0xDA, 'HAZHALM_TESTING_GROUNDS'),
    (0x14, 0xD0, 'CAEDARVA_MIRE'),
    (0x27, 0x11, 'SOUTHERN_SAN_DORIA_S'),
    (0x27, 0x13, 'EAST_RONFAURE_S'),
    (0x27, 0x15, 'JUGNER_FOREST_S'),
    (0x27, 0x23, 'VUNKERL_INLET_S'),
    (0x27, 0x17, 'BATALLIA_DOWNS_S'),
    (0x27, 0x3E, 'LA_VAULE_S'),
    (0x27, 0x19, 'EVERBLOOM_HOLLOW'),
    (0x27, 0x1C, 'BASTOK_MARKETS_S'),
    (0x27, 0x1E, 'NORTH_GUSTABERG_S'),
    (0x27, 0x20, 'GRAUBERG_S'),
    (0x27, 0x25, 'PASHHOW_MARSHLANDS_S'),
    (0x27, 0x27, 'ROLANBERRY_FIELDS_S'),
    (0x27, 0x42, 'BEADEAUX_S'),
    (0x27, 0x22, 'RUHOTZ_SILVERMINES'),
    (0x27, 0x2B, 'WINDURST_WATERS_S'),
    (0x27, 0x2D, 'WEST_SARUTABARUTA_S'),
    (0x27, 0x2F, 'FORT_KARUGO_NARUGO_S'),
    (0x27, 0x32, 'MERIPHATAUD_MOUNTAINS_S'),
    (0x27, 0x34, 'SAUROMUGUE_CHAMPAIGN_S'),
    (0x27, 0x44, 'CASTLE_OZTROJA_S'),
    (0x14, 0x11, 'WEST_RONFAURE'),
    (0x14, 0x0F, 'EAST_RONFAURE'),
    (0x14, 0x51, 'LA_THEINE_PLATEAU'),
    (0x14, 0x60, 'VALKURM_DUNES'),
    (0x14, 0x01, 'JUGNER_FOREST'),
    (0x14, 0x02, 'BATALLIA_DOWNS'),
    (0x14, 0x64, 'NORTH_GUSTABERG'),
    (0x14, 0x63, 'SOUTH_GUSTABERG'),
    (0x14, 0x69, 'KONSCHTAT_HIGHLANDS'),
    (0x14, 0x2B, 'PASHHOW_MARSHLANDS'),
    (0x14, 0x07, 'ROLANBERRY_FIELDS'),
    (0x14, 0x24, 'BEAUCEDINE_GLACIER'),
    (0x14, 0x4D, 'XARCABARD'),
    (0x14, 0x3D, 'CAPE_TERIGGAN'),
    (0x14, 0x3E, 'EASTERN_ALTEPA_DESERT'),
    (0x14, 0x18, 'WEST_SARUTABARUTA'),
    (0x14, 0x27, 'EAST_SARUTABARUTA'),
    (0x14, 0x17, 'TAHRONGI_CANYON'),
    (0x14, 0x16, 'BUBURIMU_PENINSULA'),
    (0x14, 0x20, 'MERIPHATAUD_MOUNTAINS'),
    (0x14, 0x2E, 'SAUROMUGUE_CHAMPAIGN'),
    (0x14, 0x3F, 'THE_SANCTUARY_OF_ZITAH'),
    (0x14, 0x7D, 'ROMAEVE'),
    (0x14, 0x40, 'YUHTUNGA_JUNGLE'),
    (0x14, 0x41, 'YHOATOR_JUNGLE'),
    (0x14, 0x42, 'WESTERN_ALTEPA_DESERT'),
    (0x14, 0x08, 'QUFIM_ISLAND'),
    (0x14, 0x0A, 'BEHEMOTHS_DOMINION'),
    (0x14, 0x43, 'VALLEY_OF_SORROWS'),
    (0x27, 0x31, 'GHOYUS_REVERIE'),
    (0x14, 0x6F, 'RUAUN_GARDENS'),
    (0x14, 0x82, 'DYNAMIS_BEAUCEDINE'),
    (0x14, 0x83, 'DYNAMIS_XARCABARD'),
    (0x27, 0x46, 'BEAUCEDINE_GLACIER_S'),
    (0x27, 0x48, 'XARCABARD_S'),
    (0x14, 0x65, 'HORLAIS_PEAK'),
    (0x14, 0x6C, 'GHELSBA_OUTPOST'),
    (0x14, 0x1F, 'FORT_GHELSBA'),
    (0x14, 0x5E, 'YUGHOTT_GROTTO'),
    (0x14, 0x66, 'PALBOROUGH_MINES'),
    (0x14, 0x1A, 'WAUGHROON_SHRINE'),
    (0x14, 0x21, 'GIDDEUS'),
    (0x14, 0x19, 'BALGAS_DAIS'),
    (0x14, 0x2A, 'BEADEAUX'),
    (0x14, 0x28, 'QULUN_DOME'),
    (0x14, 0x68, 'DAVOI'),
    (0x14, 0x6D, 'MONASTIC_CAVERN'),
    (0x14, 0x23, 'CASTLE_OZTROJA'),
    (0x14, 0x04, 'ALTAR_ROOM'),
    (0x14, 0x44, 'THE_BOYAHDA_TREE'),
    (0x14, 0x37, 'DRAGONS_AERY'),
    (0x14, 0x0C, 'MIDDLE_DELKFUTTS_TOWER'),
    (0x14, 0x0B, 'UPPER_DELKFUTTS_TOWER'),
    (0x14, 0x36, 'TEMPLE_OF_UGGALEPIH'),
    (0x14, 0x35, 'DEN_OF_RANCOR'),
    (0x14, 0x26, 'CASTLE_ZVAHL_BAILEYS'),
    (0x14, 0x50, 'CASTLE_ZVAHL_KEEP'),
    (0x14, 0x39, 'SACRIFICIAL_CHAMBER'),
    (0x27, 0x36, 'GARLAIGE_CITADEL_S'),
    (0x14, 0x5D, 'THRONE_ROOM'),
    (0x14, 0x2D, 'RANGUEMONT_PASS'),
    (0x14, 0x32, 'BOSTAUNIEUX_OUBLIETTE'),
    (0x14, 0x3B, 'CHAMBER_OF_ORACLES'),
    (0x14, 0x1D, 'TORAIMARAI_CANAL'),
    (0x14, 0x5C, 'FULL_MOON_FOUNTAIN'),
    (0x27, 0x29, 'CRAWLERS_NEST_S'),
    (0x14, 0x61, 'ZERUHN_MINES'),
    (0x14, 0x5B, 'KORROLOKA_TUNNEL'),
    (0x14, 0x5A, 'KUFTAL_TUNNEL'),
    (0x27, 0x1A, 'THE_ELDIEME_NECROPOLIS_S'),
    (0x14, 0x59, 'SEA_SERPENT_GROTTO'),
    (0x14, 0x71, 'VELUGANNON_PALACE'),
    (0x14, 0x72, 'THE_SHRINE_OF_RUAVITAU'),
    (0x14, 0xB3, 'STELLAR_FULCRUM'),
    (0x14, 0x73, 'LALOFF_AMPHITHEATER'),
    (0x14, 0x74, 'THE_CELESTIAL_NEXUS'),
    (0x14, 0x0D, 'LOWER_DELKFUTTS_TOWER'),
    (0x14, 0x7E, 'DYNAMIS_SAN_DORIA'),
    (0x14, 0x7F, 'DYNAMIS_BASTOK'),
    (0x14, 0x80, 'DYNAMIS_WINDURST'),
    (0x14, 0x81, 'DYNAMIS_JEUNO'),
    (0x14, 0x6E, 'KING_RANPERRES_TOMB'),
    (0x14, 0x62, 'DANGRUF_WADI'),
    (0x14, 0x1C, 'INNER_HORUTOTO_RUINS'),
    (0x14, 0x03, 'ORDELLES_CAVES'),
    (0x14, 0x1B, 'OUTER_HORUTOTO_RUINS'),
    (0x14, 0x6A, 'THE_ELDIEME_NECROPOLIS'),
    (0x14, 0x67, 'GUSGEN_MINES'),
    (0x14, 0x2C, 'CRAWLERS_NEST'),
    (0x14, 0x15, 'MAZE_OF_SHAKHRAMI'),
    (0x14, 0x14, 'GARLAIGE_CITADEL'),
    (0x14, 0x77, 'CLOISTER_OF_GALES'),
    (0x14, 0x75, 'CLOISTER_OF_STORMS'),
    (0x14, 0x7A, 'CLOISTER_OF_FROST'),
    (0x14, 0x4A, 'FEIYIN'),
    (0x14, 0x58, 'IFRITS_CAULDRON'),
    (0x14, 0x6B, 'QUBIA_ARENA'),
    (0x14, 0x78, 'CLOISTER_OF_FLAMES'),
    (0x14, 0x57, 'QUICKSAND_CAVES'),
    (0x14, 0x76, 'CLOISTER_OF_TREMORS'),
    (0x14, 0x79, 'CLOISTER_OF_TIDES'),
    (0x14, 0x34, 'GUSTAV_TUNNEL'),
    (0x14, 0x33, 'LABYRINTH_OF_ONZOZO'),
    (0x14, 0x4C, 'SOUTHERN_SAN_DORIA'),
    (0x14, 0x30, 'NORTHERN_SAN_DORIA'),
    (0x14, 0x52, 'PORT_SAN_DORIA'),
    (0x14, 0x22, 'CHATEAU_DORAGUILLE'),
    (0x14, 0x46, 'BASTOK_MINES'),
    (0x14, 0x56, 'BASTOK_MARKETS'),
    (0x14, 0x3C, 'PORT_BASTOK'),
    (0x14, 0x2F, 'METALWORKS'),
    (0x14, 0x3A, 'WINDURST_WATERS'),
    (0x14, 0x54, 'WINDURST_WALLS'),
    (0x14, 0x45, 'PORT_WINDURST'),
    (0x14, 0x38, 'WINDURST_WOODS'),
    (0x14, 0x55, 'HEAVENS_TOWER'),
    (0x14, 0x13, 'RULUDE_GARDENS'),
    (0x14, 0x4E, 'UPPER_JEUNO'),
    (0x14, 0x0E, 'LOWER_JEUNO'),
    (0x14, 0x06, 'PORT_JEUNO'),
    (0x14, 0x31, 'RABAO'),
    (0x14, 0x5F, 'SELBINA'),
    (0x14, 0x1E, 'MHAURA'),
    (0x14, 0x29, 'KAZHAM'),
    (0x14, 0x7B, 'HALL_OF_THE_GODS'),
    (0x14, 0x09, 'NORG'),
    (0x27, 0x4C, 'WESTERN_ADOULIN'),
    (0x27, 0x4D, 'EASTERN_ADOULIN'),
    (0x27, 0x4E, 'RALA_WATERWAYS'),
    (0x27, 0x4F, 'YAHSE_HUNTING_GROUNDS'),
    (0x27, 0x50, 'CEIZAK_BATTLEGROUNDS'),
    (0x27, 0x51, 'FORET_DE_HENNETIEL'),
    (0x27, 0x56, 'YORCIA_WEALD'),
    (0x27, 0x52, 'MORIMAR_BASALT_FIELDS'),
    (0x27, 0x57, 'MARJAMI_RAVINE'),
    (0x27, 0x5C, 'KAMIHR_DRIFTS'),
    (0x27, 0x53, 'SIH_GATES'),
    (0x27, 0x54, 'MOH_GATES'),
    (0x27, 0x55, 'CIRDAS_CAVERNS'),
    (0x27, 0x58, 'DHO_GATES'),
    (0x27, 0x5D, 'WOH_GATES'),
    (0x27, 0x12, 'OUTER_RAKAZNAR'),
    (0x27, 0x5A, 'MOG_GARDEN'),
    (0x27, 0x59, 'CELENNIA_MEMORIAL_LIBRARY'),
    (0x27, 0x5B, 'FERETORY'),
    (0x14, 0x09, 'ESCHA_ZITAH'),
    (0x27, 0x1B, 'ESCHA_RUAUN'),
    (0x27, 0x1D, 'REISENJIMA'),
]

# ---------------------------------------------------------------------------
# Parse zone_settings.sql  →  {id: "Display Name"}
# ---------------------------------------------------------------------------
def parse_zone_settings(path):
    names = {}
    pattern = re.compile(
        r"INSERT INTO `zone_settings` VALUES \(\s*(\d+)\s*,"   # zoneid
        r"[^,]+,[^,]+,[^,]+,"                                  # zonetype,ip,port
        r"\s*'([^']+)'"                                        # name
    )
    with open(path) as f:
        for line in f:
            m = pattern.search(line)
            if m:
                zone_id   = int(m.group(1))
                raw_name  = m.group(2)
                # Convert underscores to spaces for display
                display   = raw_name.replace('_', ' ')
                names[zone_id] = display
    return names

# ---------------------------------------------------------------------------
# Parse zonelines.sql  →  list of (from_zone, fx, fy, to_zone, tx, ty)
# where fx/fy/tx/ty are Ashita LocalPosition.X / .Y (horizontal plane only)
# ---------------------------------------------------------------------------
def parse_zonelines(path):
    transitions = []
    pattern = re.compile(
        r"INSERT INTO `zonelines` VALUES \("
        r"\d+,"            # zonelineid
        r"(\d+),"          # from_zone
        r"([^,]+),"        # from_pos_x
        r"([^,]+),"        # from_pos_y  (elevation in server space)
        r"([^,]+),"        # from_pos_z  (north/south in server space)
        r"(\d+),"          # to_zone
        r"([^,]+),"        # to_pos_x
        r"([^,]+),"        # to_pos_y
        r"([^,]+),"        # to_pos_z
    )
    with open(path) as f:
        for line in f:
            m = pattern.search(line)
            if m:
                from_z   = int(m.group(1))
                fpx      = float(m.group(2))
                fpy      = float(m.group(3))
                fpz      = float(m.group(4))
                to_z     = int(m.group(5))
                tpx      = float(m.group(6))
                tpy      = float(m.group(7))
                tpz      = float(m.group(8))

                # Convert to Ashita LocalPosition (horizontal plane).
                # Server pos_z is south-positive; Ashita LocalPosition.Y is also
                # south-positive — no sign flip needed (unlike the navmesh Z axis).
                fx = fpx;  fy = fpz
                tx = tpx;  ty = tpz

                transitions.append((from_z, fx, fy, to_z, tx, ty))
    return transitions

# ---------------------------------------------------------------------------
# Emit Lua
# ---------------------------------------------------------------------------
def lua_str(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'

def build_at_lookup(zone_names):
    """
    Build (groupId*256+messageId) -> zone_id mapping from AT_ZONE_LIST.
    Maps xi.zone.NAME constants to zone IDs by converting the constant name
    to the zone_settings.sql display name format and looking it up.
    """
    # Build reverse lookup: normalised display name -> zone_id
    name_to_id = {}
    for zid, name in zone_names.items():
        # zone_settings names use underscores; xi.zone uses same naming but
        # S-zones use _S suffix while zone_settings uses _[S].
        key = name.upper().replace(' ', '_').replace("'", '').replace('-', '_')
        key = key.replace('_[S]', '_S').replace('[S]', '_S')
        name_to_id[key] = zid

    at_lookup = {}
    unresolved = []
    for (group, msg, xi_name) in AT_ZONE_LIST:
        key = group * 256 + msg
        if key in at_lookup:
            continue  # skip duplicates (e.g. MINE_SHAFT_2716 appears twice)
        zone_id = name_to_id.get(xi_name)
        if zone_id is not None:
            at_lookup[key] = zone_id
        else:
            unresolved.append(xi_name)

    if unresolved:
        print(f'  Warning: {len(unresolved)} unresolved AT names: {unresolved[:5]}...')
    return at_lookup


def write_lua(zone_names, transitions, out_path):
    # Build lookup key: lowercase, underscores→spaces, strip apostrophes
    def lookup_key(name):
        return name.lower().replace("'", "")

    at_lookup = build_at_lookup(zone_names)

    # Group transitions by from_zone
    exits = defaultdict(list)
    for (from_z, fx, fy, to_z, tx, ty) in transitions:
        exits[from_z].append((fx, fy, to_z, tx, ty))

    with open(out_path, 'w') as f:
        f.write('--[[\n')
        f.write('* mapper - zone_transitions.lua\n')
        f.write('*\n')
        f.write('* Auto-generated by gen_zone_transitions.py from LandSandBoat SQL data.\n')
        f.write('* DO NOT EDIT MANUALLY — regenerate with gen_zone_transitions.py.\n')
        f.write('*\n')
        f.write('* Coordinate system: Ashita LocalPosition (X=east, Y=south/north)\n')
        f.write('* Conversion from server space: local_x = pos_x,  local_y = pos_z\n')
        f.write('--]]\n\n')
        f.write('local M = {}\n\n')

        # Zone name → ID lookup (for /mapper goto |Zone Name| x z)
        f.write('-- Zone display names indexed by zone ID\n')
        f.write('M.ZONE_NAMES = {\n')
        for zid in sorted(zone_names):
            f.write(f'    [{zid}] = {lua_str(zone_names[zid])},\n')
        f.write('}\n\n')

        # Reverse lookup: normalised name → zone ID
        f.write('-- Zone ID lookup by normalised name (lowercase, no apostrophes)\n')
        f.write('M.ZONE_IDS = {\n')
        for zid, name in sorted(zone_names.items(), key=lambda kv: kv[1]):
            key = lookup_key(name)
            f.write(f'    [{lua_str(key)}] = {zid},\n')
        f.write('}\n\n')

        # Autotranslate lookup: AT_LOOKUP[groupId*256+messageId] = zone_id
        # Autotranslate byte format in e.command: \xFD\x02\x??\x{groupId}\x{messageId}\xFD
        # (groupId is at offset +3 from \xFD, messageId at offset +4)
        f.write('-- Autotranslate lookup: AT_LOOKUP[group*256+msg] = zone_id\n')
        f.write('-- Use resolve_zone() instead of accessing directly.\n')
        f.write('M.AT_LOOKUP = {\n')
        for key in sorted(at_lookup):
            group = key >> 8
            msg   = key & 0xFF
            zid   = at_lookup[key]
            f.write(f'    [0x{key:04X}] = {zid},  -- group=0x{group:02X} msg=0x{msg:02X}\n')
        f.write('}\n\n')

        # Zone exits table
        f.write('-- Zone exits: EXITS[from_zone] = { {fx, fy, to_zone, tx, ty}, ... }\n')
        f.write('-- fx/fy = Ashita LocalPosition of the zone line trigger in from_zone\n')
        f.write('-- tx/ty = Ashita LocalPosition of arrival in to_zone\n')
        f.write('M.EXITS = {\n')
        for from_z in sorted(exits.keys()):
            f.write(f'    [{from_z}] = {{\n')
            for (fx, fy, to_z, tx, ty) in exits[from_z]:
                f.write(f'        {{fx={fx:.3f}, fy={fy:.3f}, to_zone={to_z}, tx={tx:.3f}, ty={ty:.3f}}},\n')
            f.write(f'    }},\n')
        f.write('}\n\n')

        # Helper: resolve zone name (strips |..| autotranslate wrappers, case-insensitive)
        f.write(r'''-- Resolve a zone name to a zone ID.
-- Accepts:
--   plain text name ("Port Bastok")
--   pipe-wrapped name ("|Port Bastok|")
--   raw autotranslate bytes from e.command: \xFD\x02\x??\x{group}\x{msg}\xFD
--     where group is at byte offset 4 and msg at byte offset 5 from the start
-- Returns zone_id or nil if not found.
function M.resolve_zone(name)
    if name == nil then return nil end
    -- Detect raw autotranslate byte sequence (starts with 0xFD)
    if name:byte(1) == 0xFD then
        local group = name:byte(4)
        local msg   = name:byte(5)
        if group ~= nil and msg ~= nil then
            return M.AT_LOOKUP[group * 256 + msg]
        end
        return nil
    end
    -- Strip pipe-wrapped display format (|Zone Name|)
    name = name:match('^|(.+)|$') or name
    -- Normalise: lowercase, remove apostrophes
    local key = name:lower():gsub("'", "")
    return M.ZONE_IDS[key]
end

-- Return the list of exits from a zone (or empty table).
function M.get_exits(zone_id)
    return M.EXITS[zone_id] or {}
end

-- Zone-level Dijkstra — find a sequence of zone transitions from from_zone to
-- to_zone.  start_x/start_y are the player's current position in from_zone
-- (defaults to 0,0 if omitted).
--
-- Edge cost = Euclidean distance walked across each zone (from the entry
-- point to the chosen exit).  This causes the planner to prefer routes where
-- entry and exit within each intermediate zone are geometrically close,
-- naturally avoiding routes that require crossing an impassable wall.
--
-- Returns a list of transition records { from_zone, fx, fy, to_zone, tx, ty }
-- or nil if no path exists.
-- step_cost_fn is an optional function: step_cost_fn(zone_id, from_x, from_y, exit)
--   Returns the cost (number) of walking from (from_x, from_y) to exit.fx/fy
--   within zone_id, or nil if that exit is unreachable from that position.
--   When nil is returned the exit is skipped entirely — this is how navmesh
--   walls are communicated to the planner.
--   When step_cost_fn is not provided, falls back to plain Euclidean distance
--   (no wall detection).
function M.zone_astar(from_zone, to_zone, start_x, start_y, step_cost_fn)
    if from_zone == to_zone then return {} end

    start_x = start_x or 0
    start_y = start_y or 0

    -- State key encodes zone + the specific entry position so multiple
    -- entrances to the same zone are treated as distinct states.
    local function skey(zone, x, y)
        return string.format('%d:%.2f:%.2f', zone, x, y)
    end

    local start_key = skey(from_zone, start_x, start_y)
    local best      = { [start_key] = 0 }   -- best cost to each state
    local prev      = {}                     -- prev[key] = { transition, parent_key }
    local visited   = {}
    local open      = { { key=start_key, zone=from_zone, x=start_x, y=start_y, cost=0 } }

    while #open > 0 do
        table.sort(open, function(a, b) return a.cost < b.cost end)
        local cur = table.remove(open, 1)

        if visited[cur.key] then goto continue end
        visited[cur.key] = true

        if cur.zone == to_zone then
            -- Reconstruct path
            local path = {}
            local k = cur.key
            while prev[k] do
                local entry = prev[k]
                table.insert(path, 1, entry.transition)
                k = entry.parent_key
            end
            return path
        end

        for _, exit in ipairs(M.get_exits(cur.zone)) do
            local nkey     = skey(exit.to_zone, exit.tx, exit.ty)
            local step_cost
            if step_cost_fn then
                step_cost = step_cost_fn(cur.zone, cur.x, cur.y, exit)
                if step_cost == nil then goto skip_exit end  -- unreachable per navmesh
            else
                local dx = exit.fx - cur.x
                local dy = exit.fy - cur.y
                step_cost = math.sqrt(dx*dx + dy*dy)
            end
            local new_cost = cur.cost + step_cost

            if not visited[nkey] and (best[nkey] == nil or new_cost < best[nkey]) then
                best[nkey] = new_cost
                prev[nkey] = {
                    transition = {
                        from_zone = cur.zone,
                        fx = exit.fx, fy = exit.fy,
                        to_zone   = exit.to_zone,
                        tx = exit.tx, ty = exit.ty,
                    },
                    parent_key = cur.key,
                }
                local found = false
                for _, e in ipairs(open) do
                    if e.key == nkey then
                        e.cost = new_cost
                        found = true
                        break
                    end
                end
                if not found then
                    table.insert(open, {
                        key  = nkey,
                        zone = exit.to_zone,
                        x    = exit.tx, y = exit.ty,
                        cost = new_cost,
                    })
                end
            end

            ::skip_exit::
        end

        ::continue::
    end

    return nil
end

return M
''')

    print(f'Wrote {out_path}')
    print(f'  {len(zone_names)} zone names, {len(transitions)} transitions across {len(exits)} zones')

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import os
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    zone_names  = parse_zone_settings(ZONE_SETTINGS)
    transitions = parse_zonelines(ZONELINES)
    write_lua(zone_names, transitions, OUTPUT)
