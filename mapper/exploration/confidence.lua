--[[
* nav - exploration/confidence.lua
*
* Track and update edge confidence in the discovered graph.
*
* Confidence represents how reliable a graph edge is:
*   1.0  = traversed successfully many times
*   0.5  = neutral / never tried
*   0.0  = failed repeatedly
*
* Confidence affects A* path cost (lower confidence = higher cost) and
* can suppress edges entirely below a threshold.
*
* This module delegates to zone_graph.record_success / record_failure
* for the actual storage.  Its role is to provide the higher-level policy:
*   - which node pair to attribute success/failure to after movement
*   - when to suppress or restore edges
--]]

local confidence = {}

-- Score changes per event.
confidence.SUCCESS_DELTA = 0.1  -- add on traversal success
confidence.FAILURE_DELTA = 0.2  -- subtract on traversal failure

-- Threshold below which edges are suppressed from A*.
confidence.SUPPRESSION_THRESHOLD = 0.1

-- Track in-flight traversals: { from_node_id, to_node_id }
local _active_traversal = nil

-------------------------------------------------------------------------------
-- Called when the adapter begins driving from node A toward node B.
-------------------------------------------------------------------------------
function confidence.begin_traversal(from_id, to_id)
    _active_traversal = { from_id = from_id, to_id = to_id }
end

-------------------------------------------------------------------------------
-- Called when the adapter reports the waypoint was reached (success).
-------------------------------------------------------------------------------
function confidence.on_reached(zone_graph)
    if _active_traversal == nil then return end
    zone_graph.record_success(_active_traversal.from_id, _active_traversal.to_id)
    _active_traversal = nil
end

-------------------------------------------------------------------------------
-- Called when the adapter reports stuck (failure).
-------------------------------------------------------------------------------
function confidence.on_stuck(zone_graph)
    if _active_traversal == nil then return end
    zone_graph.record_failure(_active_traversal.from_id, _active_traversal.to_id)
    _active_traversal = nil
end

-------------------------------------------------------------------------------
-- Clear active traversal state (e.g. on zone change or explicit stop).
-------------------------------------------------------------------------------
function confidence.reset()
    _active_traversal = nil
end

-------------------------------------------------------------------------------
-- Return the confidence score for a given directed edge.
-------------------------------------------------------------------------------
function confidence.get_score(zone_graph, from_id, to_id)
    if zone_graph.confidence[from_id] == nil then return 0.5 end
    return zone_graph.confidence[from_id][to_id] or 0.5
end

-------------------------------------------------------------------------------
-- Return true if this edge is suppressed (too low confidence to use).
-------------------------------------------------------------------------------
function confidence.is_suppressed(zone_graph, from_id, to_id)
    return confidence.get_score(zone_graph, from_id, to_id)
        < confidence.SUPPRESSION_THRESHOLD
end

return confidence
