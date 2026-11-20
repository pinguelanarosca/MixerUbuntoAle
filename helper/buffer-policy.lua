-- Pre-link Chrome interception. No asynchronous observer can provide this barrier.
-- SPDX-License-Identifier: MIT
local lutils = require("linking-utils")
local cutils = require("common-utils")
local args = ...
args = args and args:parse(1) or {}
local cookie = tonumber(args["initial.cookie"] or 0) % 4294967296
local minimum = tonumber(args["minimum.serial"] or 0)
local function cutoff()
  -- Core info can be unset while a script is being activated. Compare only
  -- when processing a target event, after the registry has been populated.
  local current = tonumber(Core.get_info().cookie)
  return current and current ~= cookie and 0 or minimum
end
local log = Log.open_topic("mixer-buffer")
local function chrome(p)
  local name = string.lower(p["application.name"] or "")
  local binary = string.lower(p["application.process.binary"] or "")
  local appid = string.lower(p["application.id"] or "")
  return name == "google chrome" or name == "chromium" or name == "google-chrome"
      or name == "chromium-browser" or binary == "chrome" or binary == "chromium" or binary == "google-chrome"
      or binary == "chromium-browser" or appid == "com.google.chrome" or appid == "org.chromium.chromium"
end

SimpleEventHook {
  name = "mixer/buffer-before-link",
  after = { "linking/get-filter-from-target", "linking/find-best-target" },
  before = "linking/prepare-link",
  interests = { EventInterest { Constraint { "event.type", "=", "select-target" } } },
  execute = function(event)
    local source, om, si, p, flags, target = lutils:unwrap_select_target_event(event)
    if p["media.class"] ~= "Stream/Output/Audio" or not chrome(p) then return end
    local serial = tonumber(p["object.serial"] or 0)
    if serial < cutoff() then return end -- existing streams retain their routing
    local metadata = cutils.get_default_metadata_object()
    if metadata and target then
      local tp = target.properties
      metadata:set(0, "mixer.buffer.request." .. tostring(serial), "Spa:String:JSON", Json.Object {
        serial = tostring(serial), source_id = tostring(p["node.id"]),
        target = tp["node.name"], target_id = tostring(tp["node.id"]),
      }:to_string())
    end
    -- The engine configures DSP ports and links every channel behind one barrier.
    -- Never let the standard policy start a direct-to-physical path.
    event:set_data("target", nil)
    event:stop_processing()
  end,
}:register()

SimpleEventHook {
  name = "mixer/exclude-private-buffer-defaults",
  before = { "default-nodes/find-selected-default-node", "default-nodes/find-stored-default-node", "default-nodes/find-best-default-node" },
  interests = { EventInterest { Constraint { "event.type", "=", "select-default-node" } } },
  execute = function(event)
    local candidates = event:get_data("available-nodes")
    if not candidates then return end
    local filtered = {}
    for _, p in ipairs(candidates:parse()) do
      if not p["mixer.buffer.serial"] then table.insert(filtered, Json.Object(p)) end
    end
    event:set_data("available-nodes", Json.Array(filtered))
  end,
}:register()

SimpleEventHook {
  name = "mixer/buffer-source-removed",
  interests = { EventInterest { Constraint { "event.type", "=", "node-removed" } } },
  execute = function(event)
    local p = event:get_properties()
    if p["media.class"] ~= "Stream/Output/Audio" or not chrome(p) then return end
    local metadata = cutils.get_default_metadata_object()
    if metadata then metadata:set(0, "mixer.buffer.request." .. tostring(p["object.serial"]), nil, nil) end
  end,
}:register()

log:info("Chrome buffer pre-link policy active; minimum serial=" .. tostring(minimum))
Core.timeout_add(100, function()
  local metadata = cutils.get_default_metadata_object()
  if not metadata then return true end
  metadata:set(0, "mixer.buffer.interceptor", "Spa:String:JSON", Json.Object {
    cookie = tostring(Core.get_info().cookie), minimum_serial = tostring(cutoff()),
    configured_cookie = tostring(cookie), configured_minimum = tostring(minimum),
  }:to_string())
  return false
end)
