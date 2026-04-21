/*
 * navmesh.cpp — pybind11 wrapper around Recast + Detour.
 *
 * Provides:
 *   build_navmesh(verts, tris, settings) → opaque handle
 *   find_path(handle, start, end) → list of (x,y,z) waypoints
 *
 * Matches FFXI NavMesh Builder Recast parameters.
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

namespace py = pybind11;

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

    // Compute bounding box
    float bmin[3] = { verts[0], verts[1], verts[2] };
    float bmax[3] = { verts[0], verts[1], verts[2] };
    for (int i = 0; i < nverts; i++) {
        for (int j = 0; j < 3; j++) {
            float v = verts[i*3+j];
            if (v < bmin[j]) bmin[j] = v;
            if (v > bmax[j]) bmax[j] = v;
        }
    }

    // Recast config
    rcConfig cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.cs = settings.cellSize;
    cfg.ch = settings.cellHeight;
    cfg.walkableSlopeAngle = settings.agentMaxSlope;
    cfg.walkableHeight = (int)ceilf(settings.agentHeight / cfg.ch);
    cfg.walkableClimb = (int)floorf(settings.agentMaxClimb / cfg.ch);
    cfg.walkableRadius = (int)ceilf(settings.agentRadius / cfg.cs);
    cfg.maxEdgeLen = (int)(settings.edgeMaxLen / cfg.cs);
    cfg.maxSimplificationError = settings.edgeMaxError;
    cfg.minRegionArea = settings.regionMinSize * settings.regionMinSize;
    cfg.mergeRegionArea = settings.regionMergeSize * settings.regionMergeSize;
    cfg.maxVertsPerPoly = (int)settings.vertsPerPoly;
    cfg.detailSampleDist = settings.detailSampleDist < 0.9f ? 0 : cfg.cs * settings.detailSampleDist;
    cfg.detailSampleMaxError = cfg.ch * settings.detailSampleMaxError;

    rcVcopy(cfg.bmin, bmin);
    rcVcopy(cfg.bmax, bmax);
    rcCalcGridSize(cfg.bmin, cfg.bmax, cfg.cs, &cfg.width, &cfg.height);

    rcContext ctx(false);

    // Step 1: heightfield
    rcHeightfield* solid = rcAllocHeightfield();
    if (!solid || !rcCreateHeightfield(&ctx, *solid, cfg.width, cfg.height,
                                        cfg.bmin, cfg.bmax, cfg.cs, cfg.ch)) {
        rcFreeHeightField(solid);
        throw std::runtime_error("Failed to create heightfield");
    }

    // Rasterize triangles
    std::vector<unsigned char> triAreas(ntris, 0);
    rcMarkWalkableTriangles(&ctx, cfg.walkableSlopeAngle,
                            verts, nverts, tris, ntris, triAreas.data());
    if (!rcRasterizeTriangles(&ctx, verts, nverts, tris, triAreas.data(),
                               ntris, *solid, cfg.walkableClimb)) {
        rcFreeHeightField(solid);
        throw std::runtime_error("Failed to rasterize triangles");
    }

    // Step 2: filter walkable surfaces
    rcFilterLowHangingWalkableObstacles(&ctx, cfg.walkableClimb, *solid);
    rcFilterLedgeSpans(&ctx, cfg.walkableHeight, cfg.walkableClimb, *solid);
    rcFilterWalkableLowHeightSpans(&ctx, cfg.walkableHeight, *solid);

    // Step 3: compact heightfield
    rcCompactHeightfield* chf = rcAllocCompactHeightfield();
    if (!chf || !rcBuildCompactHeightfield(&ctx, cfg.walkableHeight, cfg.walkableClimb,
                                            *solid, *chf)) {
        rcFreeHeightField(solid);
        rcFreeCompactHeightfield(chf);
        throw std::runtime_error("Failed to build compact heightfield");
    }
    rcFreeHeightField(solid);

    // Step 4: erode walkable area
    if (!rcErodeWalkableArea(&ctx, cfg.walkableRadius, *chf)) {
        rcFreeCompactHeightfield(chf);
        throw std::runtime_error("Failed to erode walkable area");
    }

    // Step 5: build regions
    if (!rcBuildDistanceField(&ctx, *chf)) {
        rcFreeCompactHeightfield(chf);
        throw std::runtime_error("Failed to build distance field");
    }
    if (!rcBuildRegions(&ctx, *chf, 0, cfg.minRegionArea, cfg.mergeRegionArea)) {
        rcFreeCompactHeightfield(chf);
        throw std::runtime_error("Failed to build regions");
    }

    // Step 6: contours
    rcContourSet* cset = rcAllocContourSet();
    if (!cset || !rcBuildContours(&ctx, *chf, cfg.maxSimplificationError, cfg.maxEdgeLen, *cset)) {
        rcFreeCompactHeightfield(chf);
        rcFreeContourSet(cset);
        throw std::runtime_error("Failed to build contours");
    }

    // Step 7: poly mesh
    rcPolyMesh* pmesh = rcAllocPolyMesh();
    if (!pmesh || !rcBuildPolyMesh(&ctx, *cset, cfg.maxVertsPerPoly, *pmesh)) {
        rcFreeCompactHeightfield(chf);
        rcFreeContourSet(cset);
        rcFreePolyMesh(pmesh);
        throw std::runtime_error("Failed to build poly mesh");
    }

    // Step 8: detail mesh
    rcPolyMeshDetail* dmesh = rcAllocPolyMeshDetail();
    if (!dmesh || !rcBuildPolyMeshDetail(&ctx, *pmesh, *chf,
                                          cfg.detailSampleDist,
                                          cfg.detailSampleMaxError, *dmesh)) {
        rcFreeCompactHeightfield(chf);
        rcFreeContourSet(cset);
        rcFreePolyMesh(pmesh);
        rcFreePolyMeshDetail(dmesh);
        throw std::runtime_error("Failed to build detail mesh");
    }

    rcFreeCompactHeightfield(chf);
    rcFreeContourSet(cset);

    for (int i = 0; i < pmesh->npolys; i++) {
        pmesh->flags[i] = 1;
    }

    // Build Detour navmesh data
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

    // Create Detour navmesh
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

    // Create query object
    result->navQuery = dtAllocNavMeshQuery();
    status = result->navQuery->init(result->navMesh, 2048);
    if (dtStatusFailed(status)) {
        throw std::runtime_error("Failed to init navmesh query");
    }

    return result;
}

static std::vector<std::tuple<float,float,float>> find_path(
    std::shared_ptr<NavMeshData> mesh,
    std::tuple<float,float,float> start,
    std::tuple<float,float,float> end,
    int max_polys = 256)
{
    if (!mesh || !mesh->navQuery)
        throw std::runtime_error("Invalid navmesh");

    float spos[3] = { std::get<0>(start), std::get<1>(start), std::get<2>(start) };
    float epos[3] = { std::get<0>(end), std::get<1>(end), std::get<2>(end) };
    float extents[3] = { 50.0f, 100.0f, 50.0f };

    dtQueryFilter filter;
    filter.setIncludeFlags(0xFFFF);
    filter.setExcludeFlags(0);

    dtPolyRef startRef, endRef;
    float startNearest[3], endNearest[3];

    mesh->navQuery->findNearestPoly(spos, extents, &filter, &startRef, startNearest);
    mesh->navQuery->findNearestPoly(epos, extents, &filter, &endRef, endNearest);

    if (!startRef || !endRef)
        return {};

    std::vector<dtPolyRef> polys(max_polys);
    int npolys = 0;

    mesh->navQuery->findPath(startRef, endRef, startNearest, endNearest,
                              &filter, polys.data(), &npolys, max_polys);

    if (npolys == 0)
        return {};

    const int maxStraight = 256;
    float straightPath[maxStraight * 3];
    unsigned char straightPathFlags[maxStraight];
    dtPolyRef straightPathPolys[maxStraight];
    int nstraightPath = 0;

    mesh->navQuery->findStraightPath(startNearest, endNearest,
                                      polys.data(), npolys,
                                      straightPath, straightPathFlags,
                                      straightPathPolys,
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
        .def_readwrite("detail_sample_max_error", &NavSettings::detailSampleMaxError);

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
          py::arg("max_polys") = 256);
}
