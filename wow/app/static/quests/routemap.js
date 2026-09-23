// Guide route map (guide.html): per zone, each step's quest giver → objective
// area(s) → turn-in, in the guide's order. No routing — the guide sets the
// order; this only draws it. Leaflet with CRS.Simple over the zone's PNG
// (from `python -m ingest.maps`), which spans exactly the 0–100 QuestieDB
// coordinate space. Anything missing (Leaflet, art, coordinates) degrades to
// a message; the checklist never depends on the map.

const ROLE = {
  giver: { color: '#e0a82e', verb: 'Accept' },
  objective: { color: '#5aa9ff', verb: 'Do' },
  turnin: { color: '#56c47a', verb: 'Turn in' },
};

const routeMap = {
  data: null, el: null, map: null, layer: null, zone: null, zones: [],
  done: new Set(), highlighted: null, onPick: null,
};

// Zone key: a UiMap id, or "z:<name>" for an area with no zone map (instances).
const zoneKey = a => (a.map ? String(a.map) : `z:${a.zone_name || a.zone}`);

// Ordered stops of one step: [{role, label, areas}] (giver + turn-in: first entity only).
function stepStops(step) {
  const r = step.route;
  if (!r) return [];
  const first = list => list.find(s => s.areas.length) || list[0];
  return [first(r.giver), ...r.objectives, first(r.turnin)].filter(Boolean);
}

// Per step: which parts have no location, for the checklist's "not mapped" note.
function unmappedParts(step) {
  const r = step.route;
  if (!r) return null;  // no route at all (new Forever quest, unavailable id)
  const missing = [];
  if (!r.giver.some(s => s.areas.length)) missing.push('quest giver');
  r.objectives.filter(s => !s.areas.length).forEach(s => missing.push(s.label));
  if (!r.turnin.some(s => s.areas.length)) missing.push('turn-in');
  return missing;
}

function zoneList(data) {
  const seen = new Map();
  data.steps.forEach((step, i) => stepStops(step).forEach(s => s.areas.forEach(a => {
    const key = zoneKey(a);
    if (!seen.has(key)) {
      const info = a.map ? data.maps[a.map] : null;
      seen.set(key, { key, name: info?.name || a.zone_name || 'Unknown zone', image: info?.image || null,
                      width: info?.width || 1002, height: info?.height || 668, steps: new Set() });
    }
    seen.get(key).steps.add(i);
  })));
  return [...seen.values()];
}

function renderRouteMap(data, el, { onPick }) {
  Object.assign(routeMap, { data, el, onPick, zones: zoneList(data) });
  if (!routeMap.zones.length) {
    el.innerHTML = `<div class="map-empty">None of these quests have known map locations yet.
      New Forever quests are mapped once their details are revealed.</div>`;
    return;
  }
  el.innerHTML = `
    <div class="zone-tabs" id="zoneTabs" role="tablist">${routeMap.zones.map(z => `
      <button role="tab" data-zone="${esc(z.key)}">${esc(z.name)} <span class="n">${z.steps.size}</span></button>`).join('')}</div>
    <div class="map-frame"><div id="routeLeaflet"></div><div class="map-empty overlay" id="mapMsg" hidden></div></div>
    <div class="legend">
      <span><i style="background:${ROLE.giver.color}"></i>Accept</span>
      <span><i style="background:${ROLE.objective.color}"></i>Objective</span>
      <span><i style="background:${ROLE.turnin.color}"></i>Turn in</span>
      <span><b class="solid"></b>within a step</span><span><b class="dashed"></b>to the next step</span>
      <span class="sub">Numbers match the steps below. Faint dots are where the objective mobs or items spawn.</span>
    </div>`;
  el.querySelectorAll('[data-zone]').forEach(b => b.addEventListener('click', () => showZone(b.dataset.zone)));
  showZone(routeMap.zones[0].key);
}

function toLatLng(z, x, y) {
  return [z.height * (1 - y / 100), z.width * x / 100];  // image origin top-left; CRS.Simple y grows upward
}

function showZone(key, focusStep = null, keepView = false) {
  const z = routeMap.zones.find(v => v.key === key);
  if (!z) return;
  routeMap.zone = z;
  routeMap.el.querySelectorAll('[data-zone]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.zone === key)));
  const msg = document.getElementById('mapMsg');
  const holder = document.getElementById('routeLeaflet');
  if (!z.image || typeof L === 'undefined') {
    if (routeMap.map) { routeMap.map.remove(); routeMap.map = null; }
    holder.style.visibility = 'hidden';
    msg.hidden = false;
    msg.innerHTML = typeof L === 'undefined'
      ? 'The map library couldn\'t load. The checklist below still works.'
      : `Map not available for ${esc(z.name)} yet.<br><span class="sub">Steps here: ${[...z.steps].map(i => i + 1).join(', ')}. The checklist below still works.</span>`;
    return;
  }
  msg.hidden = true;
  holder.style.visibility = '';
  try {
    drawZone(z, focusStep, keepView && routeMap.drawn === z.key);
  } catch (e) {
    holder.style.visibility = 'hidden';
    msg.hidden = false;
    msg.textContent = `Couldn't draw this map: ${e.message}. The checklist below still works.`;
  }
}

function drawZone(z, focusStep, keepView) {
  const bounds = [[0, 0], [z.height, z.width]];
  if (!routeMap.map) {
    routeMap.map = L.map('routeLeaflet', {
      crs: L.CRS.Simple, minZoom: -2, maxZoom: 2, zoomSnap: 0.25, zoomDelta: 0.5,
      attributionControl: false, maxBoundsViscosity: 0.8,
    });
  }
  const map = routeMap.map;
  if (routeMap.layer) routeMap.layer.remove();
  const layer = routeMap.layer = L.layerGroup().addTo(map);
  L.imageOverlay(`../${z.image}`, bounds).addTo(layer);
  map.setMaxBounds(L.latLngBounds(bounds).pad(0.25));

  const onZone = a => zoneKey(a) === z.key;
  const stacked = new Map();  // same spot (e.g. one NPC giving and taking several quests) → fan the markers out
  let prevEnd = null;
  const all = [];
  routeMap.data.steps.forEach((step, i) => {
    const stops = stepStops(step);
    const isDone = routeMap.done.has(step.quest_id);
    const hl = routeMap.highlighted === i;
    const faded = isDone && !hl;
    const path = [];
    stops.forEach(stop => {
      const role = ROLE[stop.role];
      (stop.spread || []).filter(p => p[0] === z.key * 1).forEach(([, x, y]) =>
        L.circleMarker(toLatLng(z, x, y), { radius: 2.5, stroke: false, fillColor: role.color,
          fillOpacity: faded ? 0.12 : 0.35, interactive: false }).addTo(layer));
      stop.areas.filter(onZone).forEach(a => {
        const ll = toLatLng(z, a.x, a.y);
        path.push(ll);
        const k = `${Math.round(a.x * 2)},${Math.round(a.y * 2)}`;
        const nth = stacked.get(k) || 0;
        stacked.set(k, nth + 1);
        const icon = L.divIcon({
          className: '', iconSize: [22, 22], iconAnchor: [11 - nth * 14, 11],
          html: `<div class="rm-pin${hl ? ' hl' : ''}${faded ? ' done' : ''}" style="--c:${role.color}">${i + 1}</div>`,
        });
        const name = step.quest?.name || `Quest #${step.quest_id}`;
        L.marker(ll, { icon, title: `Step ${i + 1} · ${role.verb}: ${name}`, zIndexOffset: hl ? 1000 : 0 })
          .bindPopup(`<b>Step ${i + 1}</b> · ${esc(name)}<br><span style="color:${role.color}">${role.verb}</span> ${esc(stop.label)}` +
                     (a.n > 1 ? ` <span class="sub">(${a.n} spawns)</span>` : ''))
          .on('click', () => routeMap.onPick?.(i))
          .addTo(layer);
      });
    });
    const style = { color: '#e0a82e', weight: hl ? 4 : 2.5, opacity: faded ? 0.25 : hl ? 1 : 0.8 };
    if (prevEnd && path.length) L.polyline([prevEnd, path[0]], { ...style, weight: 2, dashArray: '5 7', opacity: faded ? 0.2 : 0.55 }).addTo(layer);
    if (path.length > 1) L.polyline(path, style).addTo(layer);
    if (path.length) prevEnd = path[path.length - 1];
    all.push(...path);
  });

  const focus = focusStep == null ? null
    : stepStops(routeMap.data.steps[focusStep]).flatMap(s => s.areas.filter(onZone).map(a => toLatLng(z, a.x, a.y)));
  routeMap.drawn = z.key;
  map.invalidateSize();
  if (keepView) return;
  // Default view: the guide's points in this zone (not the whole zone), so clustered starts stay readable.
  if (focus && focus.length) map.fitBounds(L.latLngBounds(focus).pad(0.6), { maxZoom: 1 });
  else if (all.length) map.fitBounds(L.latLngBounds(all).pad(0.2), { maxZoom: 1 });
  else map.fitBounds(bounds);
}

// Checklist → map: switch to the step's first mapped zone and highlight it.
function focusStepOnMap(i) {
  routeMap.highlighted = i;
  const first = stepStops(routeMap.data.steps[i]).flatMap(s => s.areas)[0];
  if (!first) return false;
  showZone(zoneKey(first), i);
  return true;
}

function highlightOnMap(i) {
  routeMap.highlighted = i;
  if (routeMap.zone) showZone(routeMap.zone.key, null, true);
}

function setRouteDone(done) {
  routeMap.done = done;
  if (routeMap.zone && routeMap.map) showZone(routeMap.zone.key, null, true);
}
