/*
 * navmesh.cpp — pybind11 wrapper around Recast + Detour.
 *
 * Provides:
 *   build_navmesh(verts, tris, settings) → opaque handle
 *   find_path(handle, start, end) → list of (x,y,z) waypoints
 *
 * Supports solo (monolithic) and tiled navmesh builds.
 * Tiled mode processes geometry in spatial tiles with bounded memory per tile,
 * enabling large outdoor zones that would OOM as a single heightfield.
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/numpy.h>

#include <recastnavigation/Recast.h>
#include <recastnavigation/DetourNavMesh.h>
#include <recastnavigation/DetourNavMeshBuilder.h>
#include <recastnavigation/DetourNavMeshQuery.h>

#include <vector>
#include <stdexcept>
#include <cstring>
#include <cmath>
#include <algorithm>

namespace py = pybind11;

static unsigned int nextPow2(unsigned int v) {
    v--;
    v |= v >> 1; v |= v >> 2; v |= v >> 4; v |= v >> 8; v |= v >> 16;
    v++;
    return v;
}

static unsigned int ilog2(unsigned int v) {
    unsigned int r = 0;
    while (v >>= 1) r++;
    return r;
}

struct NavMeshData {
    dtNavMesh* navMesh = nullptr;
    dtNavMeshQuery* navQuery = nullptr;
    unsigned char* navData = nullptr;
    int navDataSize = 0;

    ~NavMeshData() {
        if (navQuery) dtFreeNavMeshQuery(navQuery);
        if (navMesh) dtFreeNavMesh(navMesh);
        // navData is owned by navMesh after addTile, don't free separately
    }
};

struct NavSettings {
    float cellSize = 0.4f;
    float cellHeight = 0.2f;
    float agentHeight = 1.8f;
    float agentRadius = 0.3f;
    float agentMaxClimb = 0.6f;
    float agentMaxSlope = 46.0f;
    int regionMinSize = 8;
    int regionMergeSize = 20;
    float edgeMaxLen = 12.0f;
    float edgeMaxError = 1.3f;
    float vertsPerPoly = 6.0f;
    float detailSampleDist = 6.0f;
    float detailSampleMaxError = 1.0f;
    int tileSize = 0;  // 0 = solo mesh (no tiling)
};

static void fillRcConfig(rcConfig& cfg, const NavSettings& s, int borderSize = 0) {
    memset(&cfg, 0, sizeof(cfg));
    cfg.cs = s.cellSize;
    cfg.ch = s.cellHeight;
    cfg.walkableSlopeAngle = s.agentMaxSlope;
    cfg.walkableHeight = (int)ceilf(s.agentHeight / cfg.ch);
    cfg.walkableClimb = (int)floorf(s.agentMaxClimb / cfg.ch);
    cfg.walkableRadius = (int)ceilf(s.agentRadius / cfg.cs);
    cfg.maxEdgeLen = (int)(s.edgeMaxLen / cfg.cs);
    cfg.maxSimplificationError = s.edgeMaxError;
    cfg.minRegionArea = s.regionMinSize * s.regionMinSize;
    cfg.mergeRegionArea = s.regionMergeSize * s.regionMergeSize;
    cfg.maxVertsPerPoly = (int)s.vertsPerPoly;
    cfg.detailSampleDist = s.detailSampleDist < 0.9f ? 0 : cfg.cs * s.detailSampleDist;
    cfg.detailSampleMaxError = cfg.ch * s.detailSampleMaxError;
    cfg.tileSize = s.tileSize;
    cfg.borderSize = borderSize;
}

// Build Recast pipeline from heightfield through detail mesh, returning true on success.
// On failure, all intermediate allocations are freed and false is returned.
static bool buildTilePipeline(
    rcContext& ctx, const rcConfig& cfg,
    const float* verts, int nverts,
    const int* tris, int ntris,
    rcPolyMesh*& pmesh, rcPolyMeshDetail*& dmesh)
{
    pmesh = nullptr;
    dmesh = nullptr;

    rcHeightfield* solid = rcAllocHeightfield();
    if (!solid || !rcCreateHeightfield(&ctx, *solid, cfg.width, cfg.height,
                                        cfg.bmin, cfg.bmax, cfg.cs, cfg.ch)) {
        rcFreeHeightField(solid);
        return false;
    }

    std::vector<unsigned char> triAreas(ntris, 0);
    rcMarkWalkableTriangles(&ctx, cfg.walkableSlopeAngle,
                            verts, nverts, tris, ntris, triAreas.data());
    if (!rcRasterizeTriangles(&ctx, verts, nverts, tris, triAreas.data(),
                               ntris, *solid, cfg.walkableClimb)) {
        rcFreeHeightField(solid);
        return false;
    }

    rcFilterLowHangingWalkableObstacles(&ctx, cfg.walkableClimb, *solid);
    rcFilterLedgeSpans(&ctx, cfg.walkableHeight, cfg.walkableClimb, *solid);
    rcFilterWalkableLowHeightSpans(&ctx, cfg.walkableHeight, *solid);

    rcCompactHeightfield* chf = rcAllocCompactHeightfield();
    if (!chf || !rcBuildCompactHeightfield(&ctx, cfg.walkableHeight, cfg.walkableClimb,
                                            *solid, *chf)) {
        rcFreeHeightField(solid);
        rcFreeCompactHeightfield(chf);
        return false;
    }
    rcFreeHeightField(solid);

    if (!rcErodeWalkableArea(&ctx, cfg.walkableRadius, *chf)) {
        rcFreeCompactHeightfield(chf);
        return false;
    }

    if (!rcBuildDistanceField(&ctx, *chf)) {
        rcFreeCompactHeightfield(chf);
        return false;
    }
    if (!rcBuildRegions(&ctx, *chf, cfg.borderSize, cfg.minRegionArea, cfg.mergeRegionArea)) {
        rcFreeCompactHeightfield(chf);
        return false;
    }

    rcContourSet* cset = rcAllocContourSet();
    if (!cset || !rcBuildContours(&ctx, *chf, cfg.maxSimplificationError, cfg.maxEdgeLen, *cset)) {
        rcFreeCompactHeightfield(chf);
        rcFreeContourSet(cset);
        return false;
    }

    pmesh = rcAllocPolyMesh();
    if (!pmesh || !rcBuildPolyMesh(&ctx, *cset, cfg.maxVertsPerPoly, *pmesh)) {
        rcFreeCompactHeightfield(chf);
        rcFreeContourSet(cset);
        rcFreePolyMesh(pmesh);
        pmesh = nullptr;
        return false;
    }

    dmesh = rcAllocPolyMeshDetail();
    if (!dmesh || !rcBuildPolyMeshDetail(&ctx, *pmesh, *chf,
                                          cfg.detailSampleDist,
                                          cfg.detailSampleMaxError, *dmesh)) {
        rcFreeCompactHeightfield(chf);
        rcFreeContourSet(cset);
        rcFreePolyMesh(pmesh);
        rcFreePolyMeshDetail(dmesh);
        pmesh = nullptr;
        dmesh = nullptr;
        return false;
    }

    rcFreeCompactHeightfield(chf);
    rcFreeContourSet(cset);

    for (int i = 0; i < pmesh->npolys; i++)
        pmesh->flags[i] = 1;

    return true;
}

static std::shared_ptr<NavMeshData> build_solo(
    const float* verts, int nverts,
    const int* tris, int ntris,
    const float* bmin, const float* bmax,
    const NavSettings& settings)
{
    rcConfig cfg;
    fillRcConfig(cfg, settings);
    rcVcopy(cfg.bmin, bmin);
    rcVcopy(cfg.bmax, bmax);
    rcCalcGridSize(cfg.bmin, cfg.bmax, cfg.cs, &cfg.width, &cfg.height);

    rcContext ctx(false);
    rcPolyMesh* pmesh = nullptr;
    rcPolyMeshDetail* dmesh = nullptr;

    if (!buildTilePipeline(ctx, cfg, verts, nverts, tris, ntris, pmesh, dmesh))
        throw std::runtime_error("Failed to build solo navmesh");

    dtNavMeshCreateParams params;
    memset(&params, 0, sizeof(params));
    params.verts = pmesh->verts;
    params.vertCount = pmesh->nverts;
    params.polys = pmesh->polys;
    params.polyAreas = pmesh->areas;
    params.polyFlags = pmesh->flags;
    params.polyCount = pmesh->npolys;
    params.nvp = pmesh->nvp;
    params.detailMeshes = dmesh->meshes;
    params.detailVerts = dmesh->verts;
    params.detailVertsCount = dmesh->nverts;
    params.detailTris = dmesh->tris;
    params.detailTriCount = dmesh->ntris;
    params.walkableHeight = settings.agentHeight;
    params.walkableRadius = settings.agentRadius;
    params.walkableClimb = settings.agentMaxClimb;
    rcVcopy(params.bmin, pmesh->bmin);
    rcVcopy(params.bmax, pmesh->bmax);
    params.cs = cfg.cs;
    params.ch = cfg.ch;
    params.buildBvTree = true;

    unsigned char* navData = nullptr;
    int navDataSize = 0;
    if (!dtCreateNavMeshData(&params, &navData, &navDataSize)) {
        rcFreePolyMesh(pmesh);
        rcFreePolyMeshDetail(dmesh);
        throw std::runtime_error("Failed to create Detour navmesh data");
    }

    rcFreePolyMesh(pmesh);
    rcFreePolyMeshDetail(dmesh);

    auto result = std::make_shared<NavMeshData>();
    result->navMesh = dtAllocNavMesh();
    if (!result->navMesh) {
        dtFree(navData);
        throw std::runtime_error("Failed to allocate Detour navmesh");
    }

    dtStatus status = result->navMesh->init(navData, navDataSize, DT_TILE_FREE_DATA);
    if (dtStatusFailed(status)) {
        dtFree(navData);
        throw std::runtime_error("Failed to init Detour navmesh");
    }
    result->navData = navData;
    result->navDataSize = navDataSize;

    result->navQuery = dtAllocNavMeshQuery();
    status = result->navQuery->init(result->navMesh, 2048);
    if (dtStatusFailed(status))
        throw std::runtime_error("Failed to init navmesh query");

    return result;
}

static std::shared_ptr<NavMeshData> build_tiled(
    const float* verts, int nverts,
    const int* tris, int ntris,
    const float* bmin, const float* bmax,
    const NavSettings& settings)
{
    const int ts = settings.tileSize;
    const float tcs = ts * settings.cellSize;

    int gw, gh;
    rcCalcGridSize(bmin, bmax, settings.cellSize, &gw, &gh);
    const int tw = (gw + ts - 1) / ts;
    const int th = (gh + ts - 1) / ts;

    const int walkableRadius = (int)ceilf(settings.agentRadius / settings.cellSize);
    const int borderSize = walkableRadius + 3;
    const float borderPad = borderSize * settings.cellSize;

    // Pre-bucket triangles by tile (including border overlap)
    std::vector<std::vector<int>> tileTris(tw * th);
    for (int i = 0; i < ntris; i++) {
        float txMin = verts[tris[i*3]*3], txMax = txMin;
        float tzMin = verts[tris[i*3]*3+2], tzMax = tzMin;
        for (int j = 1; j < 3; j++) {
            float vx = verts[tris[i*3+j]*3];
            float vz = verts[tris[i*3+j]*3+2];
            if (vx < txMin) txMin = vx; if (vx > txMax) txMax = vx;
            if (vz < tzMin) tzMin = vz; if (vz > tzMax) tzMax = vz;
        }

        int minTx = std::max(0, (int)floorf((txMin - bmin[0] - borderPad) / tcs));
        int maxTx = std::min(tw - 1, (int)floorf((txMax - bmin[0] + borderPad) / tcs));
        int minTy = std::max(0, (int)floorf((tzMin - bmin[2] - borderPad) / tcs));
        int maxTy = std::min(th - 1, (int)floorf((tzMax - bmin[2] + borderPad) / tcs));

        for (int ty = minTy; ty <= maxTy; ty++)
            for (int tx = minTx; tx <= maxTx; tx++)
                tileTris[ty * tw + tx].push_back(i);
    }

    int nonEmpty = 0;
    for (auto& t : tileTris) if (!t.empty()) nonEmpty++;
    printf("Tiled navmesh: %dx%d grid, %d non-empty tiles, tile=%d cells (%.1f units)\n",
           tw, th, nonEmpty, ts, tcs);

    // Compute max polys per tile respecting Detour's 32-bit poly ref.
    // Detour requires at least 10 salt bits: tileBits + polyBits <= 22.
    int maxPolysPerTile = 1024;
    {
        unsigned int tb = ilog2(nextPow2((unsigned int)(tw * th)));
        unsigned int pb = ilog2(nextPow2((unsigned int)maxPolysPerTile));
        while (tb + pb > 22 && pb > 4) {
            pb--;
            maxPolysPerTile = 1 << pb;
        }
    }

    dtNavMeshParams navParams;
    memset(&navParams, 0, sizeof(navParams));
    rcVcopy(navParams.orig, bmin);
    navParams.tileWidth = tcs;
    navParams.tileHeight = tcs;
    navParams.maxTiles = tw * th;
    navParams.maxPolys = maxPolysPerTile;

    auto result = std::make_shared<NavMeshData>();
    result->navMesh = dtAllocNavMesh();
    if (!result->navMesh)
        throw std::runtime_error("Failed to allocate navmesh");

    dtStatus status = result->navMesh->init(&navParams);
    if (dtStatusFailed(status))
        throw std::runtime_error("Failed to init tiled navmesh");

    rcContext ctx(false);
    int tilesBuilt = 0;

    for (int y = 0; y < th; y++) {
        for (int x = 0; x < tw; x++) {
            auto& triList = tileTris[y * tw + x];
            if (triList.empty()) continue;

            // Tile bounds with border padding
            float tileBmin[3], tileBmax[3];
            tileBmin[0] = bmin[0] + x * tcs - borderPad;
            tileBmin[1] = bmin[1];
            tileBmin[2] = bmin[2] + y * tcs - borderPad;
            tileBmax[0] = bmin[0] + (x + 1) * tcs + borderPad;
            tileBmax[1] = bmax[1];
            tileBmax[2] = bmin[2] + (y + 1) * tcs + borderPad;

            rcConfig cfg;
            fillRcConfig(cfg, settings, borderSize);
            cfg.width = ts + borderSize * 2;
            cfg.height = ts + borderSize * 2;
            rcVcopy(cfg.bmin, tileBmin);
            rcVcopy(cfg.bmax, tileBmax);

            // Build triangle subset for this tile
            int nTileTris = (int)triList.size();
            std::vector<int> tileTriData(nTileTris * 3);
            for (int i = 0; i < nTileTris; i++) {
                int ti = triList[i];
                tileTriData[i*3+0] = tris[ti*3+0];
                tileTriData[i*3+1] = tris[ti*3+1];
                tileTriData[i*3+2] = tris[ti*3+2];
            }

            rcPolyMesh* pmesh = nullptr;
            rcPolyMeshDetail* dmesh = nullptr;
            if (!buildTilePipeline(ctx, cfg, verts, nverts,
                                   tileTriData.data(), nTileTris,
                                   pmesh, dmesh))
                continue;

            if (pmesh->nverts == 0 || pmesh->npolys == 0) {
                rcFreePolyMesh(pmesh);
                rcFreePolyMeshDetail(dmesh);
                continue;
            }

            dtNavMeshCreateParams params;
            memset(&params, 0, sizeof(params));
            params.verts = pmesh->verts;
            params.vertCount = pmesh->nverts;
            params.polys = pmesh->polys;
            params.polyAreas = pmesh->areas;
            params.polyFlags = pmesh->flags;
            params.polyCount = pmesh->npolys;
            params.nvp = pmesh->nvp;
            params.detailMeshes = dmesh->meshes;
            params.detailVerts = dmesh->verts;
            params.detailVertsCount = dmesh->nverts;
            params.detailTris = dmesh->tris;
            params.detailTriCount = dmesh->ntris;
            params.walkableHeight = settings.agentHeight;
            params.walkableRadius = settings.agentRadius;
            params.walkableClimb = settings.agentMaxClimb;
            rcVcopy(params.bmin, pmesh->bmin);
            rcVcopy(params.bmax, pmesh->bmax);
            params.cs = cfg.cs;
            params.ch = cfg.ch;
            params.buildBvTree = true;
            params.tileX = x;
            params.tileY = y;
            params.tileLayer = 0;

            unsigned char* navData = nullptr;
            int navDataSize = 0;
            if (dtCreateNavMeshData(&params, &navData, &navDataSize)) {
                result->navMesh->addTile(navData, navDataSize, DT_TILE_FREE_DATA, 0, nullptr);
                tilesBuilt++;
            }

            rcFreePolyMesh(pmesh);
            rcFreePolyMeshDetail(dmesh);
        }

        if (th > 10 && (y + 1) % (th / 10) == 0)
            printf("  tile row %d/%d (%d tiles built)\n", y + 1, th, tilesBuilt);
    }

    printf("Built %d tiles\n", tilesBuilt);

    if (tilesBuilt == 0)
        throw std::runtime_error("No tiles built - zone has no walkable geometry");

    result->navQuery = dtAllocNavMeshQuery();
    status = result->navQuery->init(result->navMesh, 65535);
    if (dtStatusFailed(status))
        throw std::runtime_error("Failed to init navmesh query");

    return result;
}

static std::shared_ptr<NavMeshData> build_navmesh(
    py::array_t<float> verts_arr,
    py::array_t<int> tris_arr,
    const NavSettings& settings)
{
    auto verts_info = verts_arr.request();
    auto tris_info = tris_arr.request();

    if (verts_info.ndim != 2 || verts_info.shape[1] != 3)
        throw std::runtime_error("verts must be Nx3 float array");
    if (tris_info.ndim != 2 || tris_info.shape[1] != 3)
        throw std::runtime_error("tris must be Mx3 int array");

    int nverts = (int)verts_info.shape[0];
    int ntris = (int)tris_info.shape[0];
    const float* verts = static_cast<const float*>(verts_info.ptr);
    const int* tris = static_cast<const int*>(tris_info.ptr);

    float bmin[3] = { verts[0], verts[1], verts[2] };
    float bmax[3] = { verts[0], verts[1], verts[2] };
    for (int i = 0; i < nverts; i++) {
        for (int j = 0; j < 3; j++) {
            float v = verts[i*3+j];
            if (v < bmin[j]) bmin[j] = v;
            if (v > bmax[j]) bmax[j] = v;
        }
    }

    if (settings.tileSize <= 0) {
        return build_solo(verts, nverts, tris, ntris, bmin, bmax, settings);
    }

    // Release GIL for the potentially long tiled build
    py::gil_scoped_release release;
    return build_tiled(verts, nverts, tris, ntris, bmin, bmax, settings);
}

static std::vector<std::tuple<float,float,float>> find_path(
    std::shared_ptr<NavMeshData> mesh,
    std::tuple<float,float,float> start,
    std::tuple<float,float,float> end,
    int max_polys = 2048,
    int exclude_flags = 2)
{
    if (!mesh || !mesh->navQuery)
        throw std::runtime_error("Invalid navmesh");

    float spos[3] = { std::get<0>(start), std::get<1>(start), std::get<2>(start) };
    float epos[3] = { std::get<0>(end), std::get<1>(end), std::get<2>(end) };
    float extents[3] = { 50.0f, 100.0f, 50.0f };

    dtQueryFilter filter;
    filter.setIncludeFlags(0xFFFF);
    filter.setExcludeFlags(static_cast<unsigned short>(exclude_flags));

    dtPolyRef startRef, endRef;
    float startNearest[3], endNearest[3];

    mesh->navQuery->findNearestPoly(spos, extents, &filter, &startRef, startNearest);
    if (!startRef && exclude_flags != 0) {
        dtQueryFilter permissive;
        permissive.setIncludeFlags(0xFFFF);
        permissive.setExcludeFlags(0);
        mesh->navQuery->findNearestPoly(spos, extents, &permissive, &startRef, startNearest);
    }
    mesh->navQuery->findNearestPoly(epos, extents, &filter, &endRef, endNearest);

    if (!startRef || !endRef)
        return {};

    std::vector<dtPolyRef> polys(max_polys);
    int npolys = 0;

    mesh->navQuery->findPath(startRef, endRef, startNearest, endNearest,
                              &filter, polys.data(), &npolys, max_polys);

    if (npolys == 0)
        return {};

    const int maxStraight = 2048;
    std::vector<float> straightPath(maxStraight * 3);
    std::vector<unsigned char> straightPathFlags(maxStraight);
    std::vector<dtPolyRef> straightPathPolys(maxStraight);
    int nstraightPath = 0;

    mesh->navQuery->findStraightPath(startNearest, endNearest,
                                      polys.data(), npolys,
                                      straightPath.data(), straightPathFlags.data(),
                                      straightPathPolys.data(),
                                      &nstraightPath, maxStraight);

    std::vector<std::tuple<float,float,float>> result;
    result.reserve(nstraightPath);
    for (int i = 0; i < nstraightPath; i++) {
        result.emplace_back(
            straightPath[i*3],
            straightPath[i*3+1],
            straightPath[i*3+2]
        );
    }
    return result;
}

static std::vector<std::tuple<float,float,float>> get_poly_centers(
    std::shared_ptr<NavMeshData> mesh)
{
    std::vector<std::tuple<float,float,float>> centers;
    if (!mesh || !mesh->navMesh) return centers;

    const dtNavMesh* nav = mesh->navMesh;
    for (int i = 0; i < nav->getMaxTiles(); i++) {
        const dtMeshTile* tile = nav->getTile(i);
        if (!tile || !tile->header) continue;
        for (int j = 0; j < tile->header->polyCount; j++) {
            const dtPoly* poly = &tile->polys[j];
            if (poly->getType() == DT_POLYTYPE_OFFMESH_CONNECTION) continue;
            float center[3] = {0,0,0};
            for (int k = 0; k < poly->vertCount; k++) {
                const float* v = &tile->verts[poly->verts[k]*3];
                center[0] += v[0]; center[1] += v[1]; center[2] += v[2];
            }
            float inv = 1.0f / poly->vertCount;
            centers.emplace_back(center[0]*inv, center[1]*inv, center[2]*inv);
        }
    }
    return centers;
}

static int mark_polys_blocked(
    std::shared_ptr<NavMeshData> mesh,
    std::tuple<float,float,float> center,
    float radius)
{
    if (!mesh || !mesh->navQuery || !mesh->navMesh)
        throw std::runtime_error("Invalid navmesh");

    float pos[3] = { std::get<0>(center), std::get<1>(center), std::get<2>(center) };
    float halfExtents[3] = { radius, 100.0f, radius };

    dtQueryFilter filter;
    filter.setIncludeFlags(0xFFFF);
    filter.setExcludeFlags(0);

    const int MAX_POLYS = 512;
    dtPolyRef polys[MAX_POLYS];
    int polyCount = 0;

    mesh->navQuery->queryPolygons(pos, halfExtents, &filter, polys, &polyCount, MAX_POLYS);

    int blocked = 0;
    float radiusSq = radius * radius;

    for (int i = 0; i < polyCount; i++) {
        float closest[3];
        bool posOverPoly;
        dtStatus status = mesh->navQuery->closestPointOnPoly(polys[i], pos, closest, &posOverPoly);
        if (dtStatusFailed(status))
            continue;

        float dx = closest[0] - pos[0];
        float dz = closest[2] - pos[2];
        if (dx * dx + dz * dz <= radiusSq) {
            mesh->navMesh->setPolyFlags(polys[i], 1 | 2);
            blocked++;
        }
    }
    return blocked;
}

static int clear_blocked(std::shared_ptr<NavMeshData> mesh)
{
    if (!mesh || !mesh->navMesh)
        throw std::runtime_error("Invalid navmesh");

    int cleared = 0;
    const dtNavMesh* nav = mesh->navMesh;
    for (int i = 0; i < nav->getMaxTiles(); i++) {
        const dtMeshTile* tile = nav->getTile(i);
        if (!tile || !tile->header) continue;
        dtPolyRef base = nav->getPolyRefBase(tile);
        for (int j = 0; j < tile->header->polyCount; j++) {
            unsigned short flags;
            dtPolyRef ref = base | (dtPolyRef)j;
            if (dtStatusSucceed(mesh->navMesh->getPolyFlags(ref, &flags))) {
                if (flags & 2) {
                    mesh->navMesh->setPolyFlags(ref, 1);
                    cleared++;
                }
            }
        }
    }
    return cleared;
}

PYBIND11_MODULE(navmesh, m) {
    m.doc() = "Recast/Detour navmesh builder and pathfinder for FFXI";

    py::class_<NavSettings>(m, "NavSettings")
        .def(py::init<>())
        .def_readwrite("cell_size", &NavSettings::cellSize)
        .def_readwrite("cell_height", &NavSettings::cellHeight)
        .def_readwrite("agent_height", &NavSettings::agentHeight)
        .def_readwrite("agent_radius", &NavSettings::agentRadius)
        .def_readwrite("agent_max_climb", &NavSettings::agentMaxClimb)
        .def_readwrite("agent_max_slope", &NavSettings::agentMaxSlope)
        .def_readwrite("region_min_size", &NavSettings::regionMinSize)
        .def_readwrite("region_merge_size", &NavSettings::regionMergeSize)
        .def_readwrite("edge_max_len", &NavSettings::edgeMaxLen)
        .def_readwrite("edge_max_error", &NavSettings::edgeMaxError)
        .def_readwrite("verts_per_poly", &NavSettings::vertsPerPoly)
        .def_readwrite("detail_sample_dist", &NavSettings::detailSampleDist)
        .def_readwrite("detail_sample_max_error", &NavSettings::detailSampleMaxError)
        .def_readwrite("tile_size", &NavSettings::tileSize);

    py::class_<NavMeshData, std::shared_ptr<NavMeshData>>(m, "NavMeshData");

    m.def("build_navmesh", &build_navmesh,
          "Build navmesh from vertices and triangles",
          py::arg("verts"), py::arg("tris"),
          py::arg("settings") = NavSettings());

    m.def("get_poly_centers", &get_poly_centers,
          "Get center positions of all navmesh polygons",
          py::arg("mesh"));

    m.def("find_path", &find_path,
          "Find path between two points",
          py::arg("mesh"), py::arg("start"), py::arg("end"),
          py::arg("max_polys") = 2048,
          py::arg("exclude_flags") = 2);

    m.def("mark_polys_blocked", &mark_polys_blocked,
          "Mark navmesh polygons near a point as blocked",
          py::arg("mesh"), py::arg("center"), py::arg("radius"));

    m.def("clear_blocked", &clear_blocked,
          "Clear all blocked polygon flags",
          py::arg("mesh"));
}
