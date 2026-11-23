"use strict";

const $ = id => document.getElementById(id);
const demoDeparture = "2026-10-15T08:45";
const points = {origin: null, destination: null};
const ui = {data: null, selected: 0, submitted: null, request: 0, picking: null, bounds: null};
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const timeFormat = new Intl.DateTimeFormat("en-GB", {timeZone:"Asia/Kolkata", hour:"2-digit", minute:"2-digit"});
const exactFormat = new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit",second:"2-digit"});
const opens502Replay = new URLSearchParams(location.search).get("replay")==="502";
let map, routeLayer, labelLayer, roadLayer, comparisonRequest;
const svgNS = "http://www.w3.org/2000/svg";

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}
function icon(name) {
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class","ico");
  svg.setAttribute("aria-hidden","true");
  const use = document.createElementNS(svgNS, "use");
  use.setAttribute("href","#ico-"+name);
  svg.append(use);
  return svg;
}
function button(text, className, click) {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click",click);
  return node;
}
function duration(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n)) return "—";
  if (n > 0 && n < 30) return "<1 min";
  const mins = Math.round(n / 60);
  return mins >= 60 ? Math.floor(mins/60)+" h"+(mins%60 ? " "+mins%60+" min" : "") : mins+" min";
}
function waitDuration(seconds) {
  return seconds > 0 && seconds < 60 ? segmentTime(seconds) : duration(seconds);
}
function timeText(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "—" : timeFormat.format(date);
}
function exactTime(value) {
  return value && Number.isFinite(new Date(value).valueOf())?exactFormat.format(new Date(value)):"—";
}
function arrivalText(start,end) {
  const day=value=>Math.floor((Date.parse(value)+330*60000)/86400000);
  return timeText(end)+(day(end)>day(start)?" (+1 day)":"");
}
function timeRange(start,end) {
  if (timeText(start)!==timeText(end) || start===end) return timeText(start)+"–"+arrivalText(start,end);
  const seconds=new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit",second:"2-digit"});
  return seconds.format(new Date(start))+"–"+seconds.format(new Date(end));
}
function segmentTime(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const n=Math.round(seconds);
  if (n<60) return n+" s";
  return Math.floor(n/60)+" min"+(n%60?" "+n%60+" s":"");
}
function distanceText(metres) {
  return metres<1000?Math.round(metres)+" m":(metres/1000).toFixed(2)+" km";
}
function pedestrianAccessNote(walk) {
  const notes=[];
  if (walk.uses_steps) notes.push("This walk uses stairs.");
  if (walk.inferred_crossing_access_m>0) {
    notes.push("About "+distanceText(walk.inferred_crossing_access_m)+" of the approach to the crossing is estimated. Check the entrance on arrival.");
  }
  return notes.join(" ");
}
function localNow() {
  return new Date(Date.now()+330*60000).toISOString().slice(0,16);
}
function sourceName(text) {
  const archived=ui.submitted?.replay || ui.submitted?.depart?.startsWith("2026-10-15");
  const labels = {gps_historical:"Historical GPS", gps_historical_recent:"Historical GPS + recent passages",
    gps_live_segment:"Live GPS segment estimate", upstream_gps:"Approaching bus GPS",
    live_inferred:archived?"Inferred from an archived stop prediction":"Inferred from a live stop prediction", same_location:"Same location",
    timetable:"Timetable", schedule:"Timetable", live:archived?"Archived vehicle feed":"Live service update", same_stop:"Same stop",
    replay:"Archived GPS", gps_replay:"Archived GPS", ordered_stops:"Stop sequence",
    gtfs_shape:"Published route shape", gps:"GPS", osm:"OpenStreetMap footpaths"};
  return [...new Set(String(text || "").split(",").map(s => s.trim()).filter(Boolean)
    .map(s => labels[s] || s.replaceAll("_"," ")))].join(" · ");
}
async function getJSON(path, params={}, signal) {
  const url = new URL(path, location.origin);
  Object.entries(params).forEach(([k,v]) => url.searchParams.set(k,v));
  const response = await fetch(url,{signal,headers:{Accept:"application/json"}});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const explanation=[data.detail,data.error,data.message].find(value=>typeof value==="string");
    throw Error(explanation || "Request failed ("+response.status+").");
  }
  return data;
}
function showMessage(text, isError=false) {
  $("message").textContent=text;
  $("message").classList.toggle("error", isError);
}
function markReplay() {
  $("replay-active").hidden = $("depart").value !== demoDeparture;
}
function viewDetails() {
  const panel=$("details");
  panel.focus({preventScroll:true});
  panel.scrollIntoView({block:"start",behavior:reducedMotion?"instant":"smooth"});
}
function viewOptions() {
  $("options").focus({preventScroll:true});
  $("options").scrollIntoView({block:"start",behavior:reducedMotion?"instant":"smooth"});
}
function resultContext(departure) {
  const node=$("result-context");
  node.hidden=!departure;
  if (!departure) return;
  const prefix=ui.submitted?.replay?"Archived live feed":departure.startsWith(demoDeparture) ? "Historical departure" :
    departure.slice(0,10)<localNow().slice(0,10) ? "Historical departure" : "Departure";
  const day=new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",day:"numeric",month:"short",year:"numeric"})
    .format(new Date(departure));
  node.textContent=prefix+" · "+day+" at "+(ui.submitted?.replay?exactTime(departure):timeText(departure))+" IST";
}
function setPoint(which, item) {
  points[which] = item;
  $(which).value = item.name;
  $(which).removeAttribute("aria-invalid");
}
function endpointValue(id) {
  return points[id] && $(id).value === points[id].name
    ? String(points[id].id ?? (points[id].lat+","+points[id].lon)) : $(id).value.trim();
}

function enableStopSearch(id) {
  const input=$(id), list=$(id+"-list");
  let items=[], chosen=-1, timer, searchController;
  function close() {
    list.hidden=true;
    input.setAttribute("aria-expanded","false");
    input.removeAttribute("aria-activedescendant");
    chosen=-1;
  }
  function select(i) {
    if (items[i]) setPoint(id,items[i]);
    close();
  }
  function highlight(i) {
    chosen=i;
    Array.from(list.children).forEach((node,j) => node.setAttribute("aria-selected",String(i===j)));
    if (list.children[i]) {
      input.setAttribute("aria-activedescendant",list.children[i].id);
      list.children[i].scrollIntoView({block:"nearest"});
    }
  }
  function refresh() {
    clearTimeout(timer);
    if (searchController) searchController.abort();
    close();
    const query=input.value.trim();
    if (query.length<2 || /^[-+]?\d+(?:\.\d+)?\s*,/.test(query)) return;
    timer=setTimeout(async () => {
      searchController=new AbortController();
      try {
        const data=await getJSON("/stops",{q:query},searchController.signal);
        if (input.value.trim()!==query || document.activeElement!==input) return;
        items=data.stops || [];
        list.replaceChildren();
        items.forEach((item,i) => {
          const li=element("li",null,item.name);
          li.id=id+"-suggestion-"+i;
          li.setAttribute("role","option");
          li.setAttribute("aria-selected","false");
          li.append(element("small",null,"Stop "+item.id+" · "+Number(item.lat).toFixed(3)+", "+Number(item.lon).toFixed(3)));
          li.addEventListener("pointerdown", e => e.preventDefault());
          li.addEventListener("click", () => select(i));
          list.append(li);
        });
        list.hidden=!items.length;
        input.setAttribute("aria-expanded",String(Boolean(items.length)));
      } catch { close(); }
    },200);
  }
  input.addEventListener("input",() => {points[id]=null; input.removeAttribute("aria-invalid");refresh();});
  input.addEventListener("blur",() => setTimeout(close,100));
  input.addEventListener("keydown",event => {
    if (list.hidden) return;
    if (event.key==="ArrowDown" || event.key==="ArrowUp") {
      event.preventDefault();
      const next=chosen<0 ? (event.key==="ArrowDown"?0:items.length-1) :
        (chosen+(event.key==="ArrowDown"?1:-1)+items.length)%items.length;
      highlight(next);
    } else if (event.key==="Enter" && chosen>=0) {
      event.preventDefault();
      select(chosen);
    } else if (event.key==="Escape") close();
  });
}

// Context labels are geographic landmarks; trip endpoints and stops come from the API.
const places = [
  ["New Delhi",28.625,77.199,"city",10],["OLD DELHI",28.657,77.220,"",11],
  ["CIVIL LINES",28.680,77.217,"",12],["KAROL BAGH",28.650,77.188,"",12],
  ["DARYAGANJ",28.645,77.248,"",12],["CONNAUGHT PLACE",28.632,77.219,"",12],
  ["CHANAKYAPURI",28.596,77.177,"",12],["LODHI COLONY",28.583,77.222,"",12],
  ["LAJPAT NAGAR",28.570,77.242,"",12],["NEHRU PLACE",28.548,77.253,"",12],
  ["HAUZ KHAS",28.551,77.205,"",12],["MAYUR VIHAR",28.606,77.293,"",12],
  ["Red Fort",28.657,77.240,"landmark",13],["India Gate",28.613,77.229,"landmark",12],
  ["Lodhi Garden",28.593,77.219,"landmark",13],["Humayun’s Tomb",28.593,77.250,"landmark",13],
  ["Lotus Temple",28.553,77.258,"landmark",13],["Yamuna",28.627,77.268,"water",11]
];
function updateLabels() {
  labelLayer.clearLayers();
  const zoom=map.getZoom();
  places.forEach(([name,lat,lon,kind,minZoom]) => {
    if (zoom<minZoom) return;
    const label=element("div","place-label "+kind,name);
    L.marker([lat,lon],{pane:"atlasLabels",interactive:false,keyboard:false,
      icon:L.divIcon({html:label,className:"",iconSize:[110,18],iconAnchor:[55,9]})}).addTo(labelLayer);
  });
}
function lockMapToRoute() {
  if (!map) return;
  if (ui.bounds && ui.bounds.isValid()) {
    map.fitBounds(ui.bounds,{padding:[45,44],maxZoom:15,animate:!reducedMotion});
  } else map.setView([28.618,77.224],12);
}
function cancelPick() {
  ui.picking=null;
  $("map-prompt").hidden=true;
  $("map").classList.remove("is-picking");
  ["origin","destination"].forEach(id => $(id+"-map").setAttribute("aria-pressed","false"));
}
function pickPoint(latlng) {
  if (!ui.picking) return;
  const target=ui.picking, lat=Number(latlng.lat.toFixed(5)), lon=Number(latlng.lng.toFixed(5));
  setPoint(target,{id:null,lat,lon,name:lat.toFixed(5)+", "+lon.toFixed(5)});
  cancelPick();
  if (routeLayer) {
    L.circleMarker([lat,lon],{radius:6,color:"#fff",fillColor:"#4246a5",fillOpacity:1,weight:2})
      .addTo(routeLayer).bindTooltip(target==="origin" ? "Selected origin" : "Selected destination").openTooltip();
  }
  $(target+"-map").focus({preventScroll:true});
}
function startPick(target) {
  if (!map) {
    showMessage("Map selection is unavailable. Enter a stop or latitude, longitude instead.",true);
    return;
  }
  if (ui.picking===target) {cancelPick();return;}
  cancelPick();
  ui.picking=target;
  $(target+"-map").setAttribute("aria-pressed","true");
  $("map-prompt").firstElementChild.textContent="Choose your "+(target==="origin"?"starting point":"destination")+
    ". Tap the map, or pan with arrow keys and press Enter.";
  $("map-prompt").hidden=false;
  $("map").classList.add("is-picking");
  $("map").focus({preventScroll:true});
  if (window.innerWidth<=700) $("map-section").scrollIntoView({block:"center",behavior:reducedMotion?"instant":"smooth"});
}
async function initializeMap() {
  if (!window.L) {
    $("map-unavailable").textContent="Map unavailable. Journey search and directions are available below.";
    $("map-unavailable").hidden=false;
    $("fit-map").disabled=true;
    return;
  }
  map=L.map("map",{zoomControl:false,preferCanvas:true,minZoom:9,maxZoom:18,zoomAnimation:!reducedMotion});
  map.setView([28.618,77.224],12);
  map.createPane("atlasRoads").style.zIndex=330;
  map.createPane("atlasLabels").style.zIndex=360;
  map.createPane("atlasRoutes").style.zIndex=400;
  map.getPane("atlasLabels").style.pointerEvents="none";
  labelLayer=L.layerGroup().addTo(map);
  routeLayer=L.layerGroup().addTo(map);
  roadLayer=L.layerGroup().addTo(map);
  L.control.zoom({position:"topright"}).addTo(map);
  map.attributionControl.setPrefix(false);
  map.attributionControl.addAttribution('© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>');
  map.on("zoomend",updateLabels);
  map.on("click",event => pickPoint(event.latlng));
  $("map").addEventListener("keydown",event => {
    if (ui.picking && event.key==="Enter") {event.preventDefault();pickPoint(map.getCenter());}
    if (event.key==="Escape") cancelPick();
  });
  updateLabels();
  if (window.ResizeObserver) new ResizeObserver(() => map.invalidateSize()).observe($("map"));
  try {
    const geo=await getJSON("/map/context");
    if (!geo.features?.length) throw Error("Empty basemap");
    const styles = {river:{weight:5,color:"#9fc8d4",fillColor:"#aed0da",fillOpacity:.85},
      arterial:{weight:2.5,color:"#b4c2bd"},street:{weight:.9,color:"#c6d1cc"}};
    const rank = {river:0,street:1,arterial:2};
    const ordered={...geo,features:[...geo.features].sort((a,b) =>
      (rank[a.properties?.kind]??1) - (rank[b.properties?.kind]??1))};
    L.geoJSON(ordered,{pane:"atlasRoads",interactive:false,
      style:f => ({pane:"atlasRoads",lineCap:"round",opacity:1,...(styles[f.properties?.kind] || styles.street)})}).addTo(roadLayer);
    L.geoJSON(ordered,{pane:"atlasRoads",interactive:false,filter:f=>f.properties?.kind==="arterial",
      style:{pane:"atlasRoads",color:"#fcfcf7",weight:1.1,opacity:.92}}).addTo(roadLayer);
  } catch {
    $("map-section").querySelector(".map-label span").textContent="Street background unavailable";
  }
}
function endpointMarker(point, which) {
  if (!point || !Number.isFinite(Number(point.lat)) || !Number.isFinite(Number(point.lon))) return;
  const position=[Number(point.lat),Number(point.lon)];
  const marker=element("div","pin-marker "+which);
  marker.append(element("span",null,which==="both" ? "A/B" : which==="origin" ? "A" : "B"));
  const title=(which==="both"?"Origin and destination: ":which==="origin"?"From ":"To ")+point.name;
  const label=element("span",null,point.name);
  L.marker(position,{title,alt:title,icon:L.divIcon({html:marker,className:"",iconSize:[29,29],iconAnchor:[5,26]})})
    .addTo(routeLayer).bindTooltip(label,{className:"endpoint-label",permanent:true,direction:which==="destination"?"left":"right",offset:which==="destination"?[-2,-12]:[15,-14]});
}
function sameLocation(journey) {
  if (!journey || journey.duration_seconds!==0) return false;
  if (journey.legs?.some(leg=>leg.source==="same_location")) return true;
  const a=ui.data?.origin,b=ui.data?.destination;
  return Boolean(a && b && a.lat===b.lat && a.lon===b.lon && !(journey.legs || []).some(leg=>leg.mode==="bus"));
}
function routeLegend(journey) {
  const box=$("map-keys");
  box.replaceChildren();
  const buses=(journey?.legs || []).filter(leg=>leg.mode==="bus");
  function key(label,style) {
    const item=element("span");
    item.append(element("i",style),label);
    box.append(item);
  }
  if (buses.length) buses.forEach((leg,i)=>key(leg.route || "Bus",i%2?"key-bus second":"key-bus"));
  else if (!journey) key("Bus","key-bus");
  if (!journey || journey.legs?.some(leg=>leg.mode==="walk" && leg.seconds>0)) key("Walk","key-walk");
  if (buses.some(leg=>leg.geometry_source==="ordered_stops")) key("Approximate path","key-approx");
}
function selectedSummary(journey) {
  const panel=$("route-summary");
  panel.replaceChildren();
  panel.hidden=!journey || sameLocation(journey);
  if (panel.hidden) return;
  const buses=journey.legs.filter(leg=>leg.mode==="bus");
  const onFoot=journey.kind==="walk_only";
  const conditional=journey.boarding_status==="only_if_bus_is_present";
  const clock=journey.boardingAlert?exactTime:timeText;
  const head=element("div","summary-head");
  head.append(element("span","summary-kicker",conditional?"If the bus is still boarding":journey.boardingAlert?"Later bus":onFoot?"Walking route":"Selected journey"),
    element("span","summary-direct",onFoot?distanceText(journey.legs[0].distance_m):
      journey.transfers?journey.transfers+" transfer"+(journey.transfers>1?"s":""):"Direct"));
  const destination=journey.boardingAlert && !journey.egress_walk_seconds?buses.at(-1)?.to?.name:ui.data.destination?.name || "Destination";
  const arrival=element("div","summary-arrival");
  arrival.append(element("strong",null,clock(journey.arrival)),element("span",null,
    (conditional?"arrival if boarded":arrivalText(journey.departure,journey.arrival)===timeText(journey.arrival)?"arrival":"next-day arrival")+" · "+duration(journey.duration_seconds)+" total"));
  const stages=element("div","stage-track");
  stages.setAttribute("aria-hidden","true");
  let nBus=0;
  for (const leg of journey.legs) {
    if (leg.seconds<=0) continue;
    const segment=element("span","stage "+leg.mode+(leg.mode==="bus" && nBus++%2?" second":""));
    segment.style.flexGrow=String(leg.seconds);
    stages.append(segment);
  }
  const board=buses[0];
  const boardLine=element("div","summary-board");
  if (board) {
    boardLine.append(icon("bus"),element("span",null,conditional?
      board.route+" departure forecast "+exactTime(journey.boardingAlert.predicted_departure):
      "Board "+(board.route || "bus")+" at "+clock(board.departure)));
  } else if (onFoot) {
    boardLine.append(icon("walk"),element("span",null,"Assumed pace "+journey.legs[0].assumed_pace_m_s+" m/s"));
  }
  const directions=button("View directions","summary-action",viewDetails);
  directions.append(icon("next"));
  panel.append(head,arrival,element("div","summary-place",destination),stages,boardLine,directions);
}
function showRoute(journey) {
  routeLegend(journey);
  selectedSummary(journey);
  if (!map) return;
  routeLayer.clearLayers();
  const bounds=L.latLngBounds([]);
  let bus=0,transferCount=0;
  (journey?.legs || []).forEach(leg => {
    const coords=(leg.geometry || []).filter(c => Array.isArray(c) && c.length>=2 && c.every(Number.isFinite));
    if (coords.length<2 || leg.mode==="wait") return;
    const walking=leg.mode==="walk";
    const color=walking ? "#315f7b" : (bus++%2 ? "#454aa6" : "#d85621");
    const approximate=!walking && leg.geometry_source==="ordered_stops";
    L.polyline(coords,{pane:"atlasRoutes",weight:walking?7:8,color:"#fff",opacity:.94,interactive:false}).addTo(routeLayer);
    const line=L.polyline(coords,{pane:"atlasRoutes",color,weight:walking?4:5.5,dashArray:walking?"2 7":approximate?"9 6":undefined,
      opacity:1,lineCap:"round",lineJoin:"round"}).addTo(routeLayer);
    line.bindTooltip(element("span",null,walking?"Walk to "+leg.to.name:"Bus "+leg.route+" to "+leg.to.name+(approximate?" · Approximate path":"")));
    bounds.extend(line.getBounds());
    if (!walking) {
      for (const stop of [leg.from,leg.to]) {
        if (stop?.lat==null) continue;
        L.circleMarker([stop.lat,stop.lon],{pane:"atlasRoutes",radius:4,color,weight:2,fillColor:"#fff",fillOpacity:1})
          .addTo(routeLayer).bindTooltip(element("span",null,stop.name));
      }
      if (++transferCount>1 && leg.from?.lat!=null) {
        const label=element("span",null,"Change to "+leg.route);
        L.circleMarker([leg.from.lat,leg.from.lon],{pane:"atlasRoutes",radius:6.5,weight:3,color:"#fff",fillColor:color,fillOpacity:1})
          .addTo(routeLayer).bindTooltip(label,{className:"transfer-label",permanent:true,direction:"right",offset:[9,5]});
      }
    }
  });
  const shared=sameLocation(journey);
  for (const id of shared?["origin"]:["origin","destination"]) {
    const point=ui.data?.[id];
    if (point?.lat!=null && point?.lon!=null) {
      endpointMarker(point,shared?"both":id);
      bounds.extend([point.lat,point.lon]);
    }
  }
  ui.bounds=bounds;
  lockMapToRoute();
}
function routeChip(leg,i) {
  const chip=element("span","route-chip"+(i%2?" alt":""));
  chip.append(icon("bus"),String(leg.route || leg.route_id || "Bus"));
  return chip;
}
function sameBoardingJourney(journey, other) {
  if (!journey?.legs?.length || journey.legs.length!==other?.legs?.length || journey.arrival!==other.arrival) return false;
  return journey.legs.every((leg,i)=>{
    const otherLeg=other.legs[i];
    return leg.mode===otherLeg.mode && leg.departure===otherLeg.departure && leg.arrival===otherLeg.arrival &&
      leg.from?.id===otherLeg.from?.id && leg.to?.id===otherLeg.to?.id && leg.trip_id===otherLeg.trip_id &&
      leg.live_run_id===otherLeg.live_run_id;
  });
}
function choices() {
  const box=$("alternatives");
  box.replaceChildren();
  const options=ui.data?.journeys || [];
  const atDestination=options.length===1 && sameLocation(options[0]);
  const hasWalk=!!ui.data?.walking_alternative;
  const hasNearby=!!ui.data?.direct_from_nearby_platforms?.length;
  $("mobile-results").hidden=!options.length && !hasWalk && !ui.boarding && !hasNearby;
  $("mobile-results").textContent=hasNearby && !options.length && !hasWalk?"View nearby buses ↓":
    ui.boarding?"View bus options ↓":hasWalk?(options.length?"View options ↓":"View walk ↓"):
    atDestination?"View result ↓":"View "+options.length+" journey"+(options.length!==1?"s":"")+" ↓";
  $("option-count").hidden=!options.length || atDestination || !!ui.boarding;
  $("option-count").textContent=String(options.length);
  if (atDestination) {
    const card=element("div","at-destination");
    card.append(icon("pin"),element("span",null,(ui.data.destination?.name || "Selected location")+" · 0 min"));
    box.append(card);
    return;
  }
  options.forEach((journey,index) => {
    if (sameBoardingJourney(journey,ui.boarding?.fallback_itinerary)) return;
    const selected=index===ui.selected;
    const choice=button("","journey-choice",() => selectJourney(index));
    choice.dataset.index=String(index);
    choice.setAttribute("aria-pressed",String(selected));
    choice.setAttribute("aria-label","Journey "+(index+1)+", arrival "+arrivalText(journey.departure,journey.arrival)+", "+duration(journey.duration_seconds));
    const top=element("div","choice-top");
    const arrival=element("span","choice-time");
    arrival.append(element("small",null,"Arrive")," "+arrivalText(journey.departure,journey.arrival));
    top.append(arrival,element("span","choice-duration",duration(journey.duration_seconds)));
    const bottom=element("div","choice-bottom"), chain=element("span","route-chain");
    const buses=(journey.legs || []).filter(l=>l.mode==="bus");
    if (buses.length) choice.setAttribute("aria-label","Journey "+(index+1)+", arrival "+arrivalText(journey.departure,journey.arrival)+
      ", "+duration(journey.duration_seconds)+", board "+buses[0].route+" at "+timeText(buses[0].departure)+
      ", total wait "+waitDuration(journey.wait_seconds));
    buses.forEach((leg,i) => chain.append(routeChip(leg,i)));
    if (!chain.children.length) chain.append(icon("walk"),"Walk");
    let serviceType=journey.transfers>0?journey.transfers+" transfer"+(journey.transfers===1?"":"s"):"Direct";
    if (!journey.transfers && options[0].transfers>0) {
      const difference=journey.duration_seconds-options[0].duration_seconds;
      serviceType=difference>0?"Direct · "+duration(difference)+" later":"Direct · same arrival";
    }
    bottom.append(chain,element("span","subtle",serviceType));
    choice.append(top,bottom);
    if (buses.length) choice.append(element("div","choice-boarding","Board "+timeText(buses[0].departure)+
      " · "+waitDuration(journey.wait_seconds)+" total wait"));
    const others=buses[0]?.same_platform_services?.length;
    if (others) choice.append(element("div","choice-boarding",others+" other direct "+(others===1?"line serves":"lines serve")+" both stops"));
    box.append(choice);
  });
}
function walkingChoice() {
  const box=$("walking-option"), walk=ui.data?.walking_alternative;
  box.hidden=!walk;
  box.replaceChildren();
  if (!walk) return;
  const action=button("","walking-choice",selectWalk);
  action.setAttribute("aria-pressed","false");
  action.setAttribute("aria-label","Show walking route, "+duration(walk.seconds)+", "+distanceText(walk.distance_m));
  const top=element("div","choice-top"), title=element("span","walking-title");
  title.append(icon("walk"),"Walk all the way");
  top.append(title,element("span","choice-duration",duration(walk.seconds)));
  action.append(top,element("p",null,distanceText(walk.distance_m)+" · Arrive "+arrivalText(walk.departure,walk.arrival)));
  const faster=Number.isFinite(walk.time_saved_seconds);
  const difference=faster?duration(walk.time_saved_seconds)+" faster than the quickest bus option":"Mapped walking route";
  const comparison=element("small",null,difference);
  comparison.id="walking-comparison";
  action.append(comparison);
  action.setAttribute("aria-describedby",comparison.id);
  if (walk.includes_arterial_without_mapped_sidewalk) {
    const caution=element("small","walking-caution","Includes a road with no mapped sidewalk.");
    caution.id="walking-caution";
    action.append(caution);
    action.setAttribute("aria-describedby",comparison.id+" "+caution.id);
  }
  box.append(action);
}
function selectWalk() {
  const walk=ui.data?.walking_alternative;
  if (!walk) return;
  ui.selected=-1;
  setBoardingPressed(null);
  $("alternatives").querySelectorAll(".journey-choice").forEach(choice=>choice.setAttribute("aria-pressed","false"));
  $("walking-option").querySelector("button")?.setAttribute("aria-pressed","true");
  showRoute({kind:"walk_only",legs:[walk],arrival:walk.arrival,departure:walk.departure,duration_seconds:walk.seconds});
  const panel=element("div","details-card walking-details");
  panel.append(element("h3",null,"Walking directions"));
  panel.append(element("p","walk-endpoints",walk.from.name+" → "+walk.to.name));
  panel.append(element("p",null,"Leave "+timeText(walk.departure)+" · Arrive "+arrivalText(walk.departure,walk.arrival)+" IST"));
  panel.append(element("p",null,distanceText(walk.distance_m)+" on the mapped pedestrian network · "+
    duration(walk.seconds)+" at an assumed ordinary pace of "+walk.assumed_pace_m_s+" m/s, including modelled crossing delays."));
  if (walk.includes_arterial_without_mapped_sidewalk) {
    panel.append(element("p","walk-footnote","Part of this walk follows a road with no mapped sidewalk."));
  }
  const accessNote=pedestrianAccessNote(walk);
  if (accessNote) panel.append(element("p","walk-footnote",accessNote));
  $("details").replaceChildren(panel);
}
function compareLink(container) {
  if (ui.submitted?.replay) return;
  const otherHour=ui.submitted?.depart?.slice(11,16)==="13:00"?8:13;
  const otherClock=String(otherHour).padStart(2,"0")+":00";
  const compare=button("Compare with a"+(otherHour===8?"n ":" ")+otherClock+" departure","text-btn",async () => {
    if (!ui.submitted) return;
    if (comparisonRequest) comparisonRequest.abort();
    comparisonRequest=new AbortController();
    compare.disabled=true;
    response.textContent="Checking the same trip at "+otherClock+" IST…";
    try {
      const data=await getJSON("/journeys",{...ui.submitted,compare_hour:String(otherHour)},comparisonRequest.signal);
      const result=data.comparison;
      response.textContent=result?.fastest_seconds!=null
        ? "Departing at "+otherClock+" takes "+duration(result.fastest_seconds)+(result.arrival?" and arrives at "+timeText(result.arrival):"")+"."
        : "No "+otherClock+" comparison is available for these stops.";
    } catch (error) {
      if (error.name!=="AbortError") response.textContent="Comparison unavailable. Try another departure above.";
    } finally {compare.disabled=false;}
  });
  const response=element("div","compare-result");
  response.setAttribute("role","status");
  container.append(compare,response);
}
function stopSchedule(leg, clock=timeText) {
  const section=element("details","bus-stops");
  const stops=leg.stops || [], calls=leg.calls || [];
  if (calls.length<2 && stops.length<2) return null;
  const count=leg.stop_count ?? Math.max(calls.length,stops.length)-1;
  section.append(element("summary",null,count+" stops"+(calls.length>1?" · times and segments":" · show stops")));
  if (calls.length<2) {
    const list=element("ol");
    stops.forEach(s=>list.append(element("li",null,s.name)));
    section.append(list);
    return section;
  }
  const names=new Map(stops.map(s=>[String(s.id),s.name]));
  const table=element("table","stop-calls");
  const heading=element("thead"), header=element("tr");
  ["Stop","Arrival IST","Prior segment"].forEach(label=>{
    const th=element("th",null,label);
    th.scope="col";
    header.append(th);
  });
  heading.append(header);
  table.append(heading);
  const body=element("tbody");
  let displaysSpeed=false;
  calls.forEach((call,index)=>{
    const tr=element("tr");
    const name=call.name || names.get(String(call.stop_id)) || String(call.stop_id);
    const stop=element("th",null,name);
    stop.scope="row";
    if (call.source) stop.append(element("small","call-source",sourceName(call.source)));
    const clockCell=element("td","call-time",call.arrival?clock(call.arrival):"—");
    if (call.departure && (!index || call.departure!==call.arrival)) {
      clockCell.append(element("small",null,"Dep "+clock(call.departure)));
    }
    const segment=element("td",null,index?segmentTime(call.preceding_segment_seconds):"Board");
    if (index && Number.isFinite(call.estimated_speed_kmh)) {
      segment.append(element("small",null,call.estimated_speed_kmh.toFixed(1)+" km/h"));
      displaysSpeed=true;
    }
    tr.append(stop,clockCell,segment);
    body.append(tr);
  });
  table.append(body);
  section.append(table);
  if (displaysSpeed) section.append(element("p","segment-note","Segment speeds are estimates and include time spent in traffic and at signals."));
  return section;
}
function platformArrival(leg) {
  const panel=element("div","platform-check");
  const asof=ui.data?.issued_asof || ui.data?.departure || ui.submitted?.depart;
  const replay=ui.submitted?.replay;
  const result=element("div","platform-result");
  result.id="platform-result";
  result.setAttribute("role","status");
  const action=button("Check next bus at this stop","text-btn",async ()=>{
    action.disabled=true;
    result.textContent="Checking the next "+leg.route+" at "+leg.from.name+"…";
    try {
      const prediction=await getJSON("/arrivals",{stop:leg.from.id,route:leg.route_id,at:asof,...(replay?{replay}:{})});
      const possible=prediction.possible_boarding;
      const clock=possible || replay?exactTime:timeText;
      const date=new Intl.DateTimeFormat("en-GB",{timeZone:"Asia/Kolkata",day:"numeric",month:"short"}).format(new Date(asof));
      const when=clock(asof)+" IST on "+date;
      const heading=prediction.arrival && Number.isFinite(prediction.wait_seconds)
        ? (possible?"Later ":"Next ")+leg.route+" at "+clock(prediction.arrival)+" · "+waitDuration(prediction.wait_seconds)+" wait"
        : "No arrival estimate available";
      result.replaceChildren(element("p",null,"For a rider already at "+leg.from.name+" at "+when+"."));
      if (possible?.predicted_departure) {
        result.append(element("strong",null,"Possible boarding · "+(possible.route || leg.route)+" forecast at "+exactTime(possible.predicted_departure)),
          element("p",null,possible.note || "The bus may already have left the platform."),
          element("p",null,boardingEvidence(possible)));
      }
      result.append(element("strong",null,heading));
      if (prediction.source || prediction.observed_at) {
        const evidence=[];
        if (prediction.source) evidence.push(sourceName(prediction.source));
        if (prediction.observed_at) {
          const observed=typeof prediction.observed_at==="number"?new Date(prediction.observed_at*1000):prediction.observed_at;
          const observedStop=prediction.observed_stop?.name;
          evidence.push("Observed "+clock(observed)+" IST"+(observedStop?" at "+observedStop:""));
        }
        result.append(element("p",null,evidence.join(" · ")));
      }
      if (prediction.note) result.append(element("p",null,prediction.note));
      const observation=prediction.route_observation;
      if (observation?.direction==="toward_platform" && observation.trip_assignment==="unmatched_start_clock" &&
          observation.observed_stop?.name && Number.isFinite(Date.parse(observation.observed_at))) {
        result.append(element("strong",null,"Bus seen upstream"),
          element("p",null,(observation.route || leg.route)+" was seen at "+observation.observed_stop.name+
            " at "+exactTime(observation.observed_at)+", travelling toward this stop. "+
            "Its timetable trip and arrival time are unconfirmed."));
      }
    } catch(error) {
      result.textContent="Platform arrivals unavailable. "+error.message;
    } finally {action.disabled=false;}
  });
  action.id="check-arrivals";
  action.setAttribute("aria-controls",result.id);
  panel.append(action,result);
  return panel;
}
function details(journey) {
  const box=$("details");
  box.replaceChildren();
  if (!journey || sameLocation(journey)) return;
  const panel=element("div","details-card"), stats=element("div","summary-stats");
  const conditional=journey.boarding_status==="only_if_bus_is_present";
  const clock=journey.boardingAlert?exactTime:timeText;
  panel.append(element("h3",null,conditional?"If you board this bus":journey.boardingAlert?"Later bus details":"Journey details"));
  if (conditional) panel.append(element("p","boarding-assumption","This estimate assumes boarding at "+exactTime(journey.boardingAlert.ready_at)+
    " while the bus is still at the platform. Downstream times move "+segmentTime(journey.shift_seconds)+" later, keeping the forecast ride duration."));
  [["walk","Walk",journey.walk_seconds],["clock","Wait",journey.wait_seconds],["bus","Ride",journey.ride_seconds]].forEach(([key,label,value])=>{
    const item=element("span");
    item.append(icon(key),label+" "+(key==="clock"?waitDuration(value):duration(value)));
    stats.append(item);
  });
  panel.append(stats);
  const stages=element("dl","walk-stages");
  function walkStage(label,seconds,zero) {
    if (!Number.isFinite(seconds)) return;
    const entry=element("div");
    entry.append(element("dt",null,label),element("dd",null,seconds?duration(seconds):zero));
    stages.append(entry);
  }
  walkStage("To first stop",journey.access_walk_seconds,"At stop");
  if (journey.transfers) walkStage("Between buses",journey.transfer_walk_seconds,"Same stop");
  walkStage("From last stop",journey.egress_walk_seconds,"At destination");
  if (stages.children.length) panel.append(stages);
  const list=element("ol","legs");
  const firstBus=journey.legs?.find(leg=>leg.mode==="bus");
  (journey.legs || []).forEach(leg => {
    const row=element("li"), marker=element("span","leg-icon "+leg.mode), body=element("div");
    marker.append(icon(leg.mode==="wait"?"clock":leg.mode));
    const header=element("div","leg-title"), start=leg.from?.name || "", end=leg.to?.name || "";
    const title=leg.mode==="bus"?"Bus "+(leg.route || leg.route_id || ""):leg.mode==="wait"?"Wait at "+start:"Walk to "+end;
    const times=journey.boardingAlert?clock(leg.departure)+"–"+clock(leg.arrival):timeRange(leg.departure,leg.arrival);
    header.append(element("span",null,title),element("time",null,times));
    body.append(header);
    const description=leg.mode==="bus"?start+" → "+end:leg.mode==="walk"?"From "+start:"";
    if (description) body.append(element("p","leg-destination",description));
    const meta=[leg.mode==="wait"?waitDuration(leg.seconds):duration(leg.seconds)];
    if (leg.distance_m>0) meta.push(leg.distance_m<1000?Math.round(leg.distance_m)+" m":(leg.distance_m/1000).toFixed(1)+" km");
    if (leg.source) meta.push(sourceName(leg.source));
    if (leg.geometry_source==="ordered_stops") meta.push("Approximate path between stops");
    body.append(element("p","leg-source",meta.join(" · ")));
    if (leg.includes_arterial_without_mapped_sidewalk) {
      body.append(element("p","leg-source","Part of this walk follows a road with no mapped sidewalk."));
    }
    if (leg.mode==="walk") {
      const accessNote=pedestrianAccessNote(leg);
      if (accessNote) body.append(element("p","leg-source",accessNote));
    }
    if (leg===firstBus && leg.from?.id && leg.route_id) body.append(platformArrival(leg));
    if (leg===firstBus && !conditional && journey.access_walk_seconds>0) {
      const margin=Math.round((Date.parse(leg.departure)-Date.parse(journey.departure))/1000)-journey.access_walk_seconds;
      if (margin>=0 && margin<=60) {
        body.append(element("p","boarding-assumption",margin===0
          ?"The walk and bus are estimated to arrive at the same time. You may miss this bus."
          :"Tight connection: the bus is due "+segmentTime(margin)+" after your estimated walk to this stop."));
      }
    }
    if (leg.same_platform_services?.length) {
      const sameStops=element("details","service-list");
      sameStops.append(element("summary",null,"Other buses from this platform to "+leg.to.name),
        element("p","leg-source","If another line arrives first, check its direction. These buses serve the same two stops. Times are estimates."),
        serviceRows(leg.same_platform_services,journey.departure));
      body.append(sameStops);
    }
    const schedule=leg.mode==="bus" ? stopSchedule(leg,clock) : null;
    if (schedule) body.append(schedule);
    else if (leg.stop_count) body.append(element("p","leg-source",leg.stop_count+" stops"));
    row.append(marker,body);
    list.append(row);
  });
  panel.append(list);
  compareLink(panel);
  box.append(panel);
}
function serviceRows(services,reference) {
  const list=element("ul","platform-services");
  for (const service of services) {
    const row=element("li",null);
    row.append(element("strong",null,service.route+" · "+arrivalText(reference,service.departure_at)),
      element("span",null," → "+(service.alighting_stop?service.alighting_stop.name+" ":"")+arrivalText(reference,service.alight_at)),
      element("small","leg-source",sourceName(service.departure_source)));
    list.append(row);
  }
  return list;
}
function nearbyDirectChoices() {
  const panel=$("nearby-direct");
  const options=ui.data?.direct_from_nearby_platforms || [];
  panel.replaceChildren();
  panel.hidden=options.length===0;
  if (!options.length) return;

  panel.append(element("h3",null,"Direct buses from nearby stops"),
    element("p","boarding-context","We could not verify a walk to these boarding platforms within the search limit. Check which side of the road you can reach. The times below start at the bus stop."));
  const groups=new Map();
  for (const service of options) {
    const id=service.boarding_stop.id;
    if (!groups.has(id)) groups.set(id,[]);
    groups.get(id).push(service);
  }
  for (const services of groups.values()) {
    const {boarding_stop:stop,straight_line_to_board_m:separation}=services[0];
    const group=element("div","nearby-stop");
    group.append(element("strong",null,stop.name),
      element("p","leg-source",distanceText(separation)+" straight-line distance from your start"));
    group.append(serviceRows(services.slice(0,3),ui.data.departure));
    if (services.length>3) {
      const more=element("details","service-list");
      more.append(element("summary",null,"Show "+(services.length-3)+" more departures"),
        serviceRows(services.slice(3),ui.data.departure));
      group.append(more);
    }
    group.append(button("Plan from this stop","text-btn",()=>{
      setPoint("origin",stop);
      $("depart").focus();
      $("depart").scrollIntoView({block:"center",behavior:reducedMotion?"instant":"smooth"});
      showMessage("Set the time you expect to be at "+stop.name+", then choose Find journeys.");
    }));
    panel.append(group);
  }
}
function selectJourney(index) {
  ui.selected=index;
  setBoardingPressed(null);
  const option=ui.data?.journeys?.[index];
  $("walking-option").querySelector("button")?.setAttribute("aria-pressed","false");
  $("alternatives").querySelectorAll(".journey-choice").forEach(b=>b.setAttribute("aria-pressed",String(Number(b.dataset.index)===index)));
  details(option);
  showRoute(option);
}
const firstChoiceMarginSeconds = 30;
function hasBoardingChoice(alert) {
  const itinerary=alert?.conditional_itinerary;
  return alert?.source==="live" && alert.catchable===false &&
      Number.isInteger(alert.seconds_before_ready) && alert.seconds_before_ready>0 && alert.seconds_before_ready<=60 &&
      alert.fallback_itinerary?.legs?.some(leg=>leg.mode==="bus") &&
      itinerary?.boarding_status==="only_if_bus_is_present" && itinerary.legs?.some(leg=>leg.mode==="bus") &&
      [alert.predicted_departure,alert.ready_at,itinerary.arrival].every(stamp=>Number.isFinite(Date.parse(stamp)));
}
function showConditionalFirst(alert) {
  return alert && alert.seconds_before_ready<=firstChoiceMarginSeconds &&
    alert.access_walk_seconds===0 && alert.vehicle_approaching===true;
}
function actionableBoarding(alerts) {
  const valid=(alerts || []).filter(hasBoardingChoice);
  valid.sort((a,b)=>{
    const aExtended=a.seconds_before_ready>firstChoiceMarginSeconds, bExtended=b.seconds_before_ready>firstChoiceMarginSeconds;
    if (aExtended!==bExtended) return aExtended?1:-1;
    return Date.parse(a.conditional_itinerary.arrival)-Date.parse(b.conditional_itinerary.arrival);
  });
  return valid[0];
}
function setBoardingPressed(key) {
  for (const choice of $("boarding-options").querySelectorAll("[data-boarding]")) {
    choice.setAttribute("aria-pressed",String(choice.dataset.boarding===key));
  }
}
function selectBoarding(key) {
  const alert=ui.boarding, journey=alert?.[key];
  if (!journey) return;
  ui.selected=null;
  $("alternatives").querySelectorAll(".journey-choice").forEach(choice=>choice.setAttribute("aria-pressed","false"));
  $("walking-option").querySelector("button")?.setAttribute("aria-pressed","false");
  setBoardingPressed(key);
  const selected={...journey,boardingAlert:alert};
  details(selected);
  showRoute(selected);
}
function boardingEvidence(alert) {
  const age=alert.gps_age_seconds;
  if (!Number.isInteger(age) || age<0) return "No recent GPS location could be matched to this bus.";
  const when=age===0?"at the time of this request":age+" second"+(age===1?"":"s")+" before this request";
  if (alert.vehicle_approaching===true) {
    return "The last two GPS fixes showed the bus moving toward this stop. The latest fix was "+when+".";
  }
  return "The last bus location was recorded "+when+". Its approach to this stop is unconfirmed.";
}
function boardingChoices() {
  const panel=$("boarding-options"), alert=ui.boarding;
  panel.hidden=!alert;
  panel.replaceChildren();
  if (!alert) return;
  const route=alert.route || "Bus", platform=alert.platform?.name || "the platform";
  const title=element("h3",null,route+" may still be boarding");
  const gap=alert.seconds_before_ready;
  const access=alert.access_walk_seconds;
  const intro=element("p","boarding-context");
  intro.append("The forecast put departure from "+platform+" at ",element("strong",null,exactTime(alert.predicted_departure)),". ");
  if (access>0) {
    const walk=alert.conditional_itinerary.legs.find(leg=>leg.mode==="walk");
    const length=walk?.distance_m>0?distanceText(walk.distance_m)+", ":"";
    intro.append("Your "+length+segmentTime(access)+" walk to the stop puts you there at ");
  } else {
    intro.append("You’re at this stop at ");
  }
  intro.append(element("strong",null,exactTime(alert.ready_at)),", "+gap+" second"+(gap===1?"":"s")+" later. The bus may already have left.");
  intro.append(" "+boardingEvidence(alert));
  const guidance=element("p","boarding-guidance","Board only if you see the bus taking passengers. If it has left, use the later option.");
  panel.append(title,intro,guidance);
  const cards=[["conditional_itinerary","If it’s still boarding"],["fallback_itinerary","If it has left"]];
  if (!showConditionalFirst(alert)) cards.reverse();
  for (const [key,label] of cards) {
    const journey=alert[key], bus=journey?.legs?.find(leg=>leg.mode==="bus");
    if (!bus) continue;
    const conditional=key==="conditional_itinerary";
    const card=button("","boarding-choice",()=>selectBoarding(key));
    card.dataset.boarding=key;
    card.setAttribute("aria-pressed","false");
    card.setAttribute("aria-controls","details map-section");
    const header=element("span","boarding-row-head");
    header.append(element("span",null,label),element("span","boarding-select"));
    header.lastChild.setAttribute("aria-hidden","true");
    const arrival=element("span","boarding-arrival");
    arrival.append("Arrive ",element("strong",null,exactTime(journey.arrival)));
    const name=journey.egress_walk_seconds?ui.data.destination?.name:journey.legs.filter(leg=>leg.mode==="bus").at(-1)?.to?.name;
    if (name) arrival.append(element("small",null,name));
    const dep=conditional?alert.predicted_departure:bus.departure;
    const boardSource=bus.calls?.[0]?.source || journey.legs.find(leg=>leg.mode==="wait")?.source;
    card.append(header,arrival,element("span","boarding-sub",bus.route+(conditional?" forecast at ":" departs at ")+exactTime(dep)+
      (conditional || !boardSource?"":" · "+sourceName(boardSource))));
    panel.append(card);
  }
  const mapLink=button("View selected route on map","text-btn boarding-map",()=>{
    $("map").focus({preventScroll:true});
    $("map-section").scrollIntoView({block:"center",behavior:reducedMotion?"instant":"smooth"});
  });
  mapLink.prepend(icon("map"));
  panel.append(mapLink);
}
function showWarnings(warnings) {
  const section=$("notes"), list=section.querySelector("ul");
  list.replaceChildren();
  const valid=[...new Set((warnings || []).filter(Boolean))];
  valid.forEach(note=>list.append(element("li",null,note)));
  section.hidden=!valid.length;
}
function showBoardingAlerts(alerts) {
  const panel=$("boarding-alerts");
  panel.replaceChildren();
  const notices=(Array.isArray(alerts)?alerts:[]).filter(alert=>
    alert!==ui.boarding && alert?.catchable===false && alert.source==="live" && typeof alert.message==="string" && alert.message.trim());
  if (!notices.length) return;
  const heading=element("h3");
  heading.append(icon("clock"),notices.length===1?"Possible missed departure":"Possible missed departures");
  panel.append(heading);
  for (const alert of notices) {
    panel.append(element("p",null,alert.message.trim()));
    if (hasBoardingChoice(alert)) {
      panel.append(button("Inspect "+alert.route+" and its later option","text-btn",()=>{
        ui.boarding=alert;
        boardingChoices();
        selectBoarding("conditional_itinerary");
        showBoardingAlerts(alerts);
        $("boarding-options").querySelector('[data-boarding="conditional_itinerary"]').focus();
      }));
    }
  }
}
async function searchJourney(submittedByRider=false) {
  const from=endpointValue("origin"), to=endpointValue("destination"), depart=$("depart").value;
  if (!from || !to || !depart) {
    const name=!from?"origin":!to?"destination":"depart";
    $(name).setAttribute("aria-invalid","true");
    $(name).focus();
    showMessage("Enter both places and an India Standard Time departure.",true);
    return;
  }
  cancelPick();
  if (comparisonRequest) comparisonRequest.abort();
  const ticket=++ui.request;
  const params={from,to,depart:depart+(depart.length===16?":00":"")+"+05:30"};
  if (opens502Replay && depart>="2026-11-21T01:17" && depart<="2026-11-21T01:30") params.replay="nov21";
  ui.data=null; ui.submitted=null; ui.boarding=null;
  choices();walkingChoice();boardingChoices();nearbyDirectChoices();details();showWarnings([]);showBoardingAlerts([]);showRoute();resultContext(null);
  $("options").setAttribute("aria-busy","true");
  $("journey-form").querySelector(".submit").disabled=true;
  showMessage("Finding catchable buses and walking connections…");
  try {
    const data=await getJSON("/journeys",params);
    if (ticket!==ui.request) return;
    ui.data=data;ui.submitted=params;ui.selected=0;
    ui.boarding=actionableBoarding(data.boarding_alerts);
    choices();walkingChoice();boardingChoices();nearbyDirectChoices();
    if (showConditionalFirst(ui.boarding)) selectBoarding("conditional_itinerary");
    else if (ui.boarding && sameBoardingJourney(data.journeys?.[0],ui.boarding.fallback_itinerary)) selectBoarding("fallback_itinerary");
    else if (!data.journeys?.length && data.walking_alternative) selectWalk();
    else selectJourney(0);
    showWarnings(data.warnings);showBoardingAlerts(data.boarding_alerts);resultContext(params.depart);
    if (!data.journeys?.length && !ui.boarding && data.direct_from_nearby_platforms?.length) {
      showMessage("The planner could not map a complete bus journey from your start. Nearby departures are listed below.");
    } else if (!data.journeys?.length && !ui.boarding) {
      const note=data.warnings?.[0];
      showMessage((note || "No bus journey found.")+(data.walking_alternative?"":" Try a different departure or a nearby stop."));
    } else if (sameLocation(data.journeys?.[0])) {
      showMessage("You’re already at your destination.");
    } else showMessage("");
  } catch(error) {
    if (ticket===ui.request) showMessage("Journey search unavailable. "+error.message,true);
  } finally {
    if (ticket===ui.request) {
      $("options").removeAttribute("aria-busy");
      $("journey-form").querySelector(".submit").disabled=false;
      if ((submittedByRider || (opens502Replay && ui.boarding)) && window.innerWidth<=700) viewOptions();
    }
  }
}
async function loadStatus() {
  const panel=$("feed-panel");
  try {
    const data=await getJSON("/status");
    const state=String(data.live?.state || "unavailable").toLowerCase();
    const live=["active","live","fresh","available","online"].includes(state);
    $("feed-text").textContent=live?"Live feed available":state==="stale"?"Live feed delayed":
      state==="fresh_no_match"?"No matched live trips":"Live feed offline";
    $("feed-short").textContent=live?"Live feed":state==="stale"?"Delayed feed":state==="fresh_no_match"?"No live match":"Offline";
    $("feed-toggle").setAttribute("aria-label","Current feed status. "+$("feed-text").textContent);
    document.querySelector(".status-dot").classList.toggle("online",live);
    panel.replaceChildren(element("strong",null,"Current feed status"),element("p",null,
      "Delhi bus timetable"+(data.feed_version?" · "+data.feed_version:"")+". "+
      (live?"Current service updates are available. Each journey leg shows the data used for its departure time.":
        "The timetable and archived GPS replay are available for journey planning.")));
    if (data.live?.timestamp) {
      const observed = new Date(data.live.timestamp*1000).toLocaleString("en-GB",{
        timeZone:"Asia/Kolkata",day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"});
      panel.append(element("small","feed-time","Feed timestamp "+observed+" IST · "+(data.live.matched_stop_updates??0).toLocaleString("en-IN")+" matched stop predictions."));
    }
  } catch {
    $("feed-text").textContent="Data status unavailable";
    $("feed-short").textContent="Unavailable";
    $("feed-toggle").setAttribute("aria-label","Current feed status unavailable");
    document.querySelector(".status-dot").classList.remove("online");
    panel.replaceChildren(element("p",null,"The planner could not check the local data service."));
  }
}
function initialize() {
  if (opens502Replay) {
    setPoint("origin",{id:"2383",name:"MMTC / Adhchini Village"});
    setPoint("destination",{id:"2386",name:"T.B. Hospital"});
    $("depart").value="2026-11-21T01:18";
  } else if (new URLSearchParams(location.search).get("example")==="aiims") {
    setPoint("origin",{name:"Laxmi Bai Nagar, near AIIMS",lat:28.568914,lon:77.213693});
    setPoint("destination",{name:"Block K, Lajpat Nagar II",lat:28.566610,lon:77.246373});
    $("depart").value="2026-11-22T15:00";
  }
  for (const id of ["origin","destination"]) {
    enableStopSearch(id);
    $(id+"-map").addEventListener("click",()=>startPick(id));
  }
  $("swap").addEventListener("click",()=>{
    [points.origin,points.destination]=[points.destination,points.origin];
    const a=$("origin"),b=$("destination");
    [a.value,b.value]=[b.value,a.value];
  });
  $("journey-form").addEventListener("submit",e=>{e.preventDefault();searchJourney(true);});
  $("now").addEventListener("click",()=>{$("depart").value=localNow();markReplay();});
  $("replay").addEventListener("click",()=>{$("depart").value=demoDeparture;markReplay();});
  $("depart").addEventListener("input",markReplay);
  $("cancel-pick").addEventListener("click",cancelPick);
  $("fit-map").addEventListener("click",lockMapToRoute);
  $("mobile-results").addEventListener("click",viewOptions);
  const status=$("feed-toggle"), closeStatus=()=>{
    status.setAttribute("aria-expanded","false");$("feed-panel").hidden=true;
  };
  status.addEventListener("click",()=>{
    const open=status.getAttribute("aria-expanded")==="true";
    status.setAttribute("aria-expanded",String(!open));$("feed-panel").hidden=open;
    if (!open) loadStatus();
  });
  document.addEventListener("click",e=>{if (!e.target.closest(".status-wrap")) closeStatus();});
  document.addEventListener("keydown",e=>{if(e.key==="Escape"){closeStatus();cancelPick();}});
  markReplay();
  initializeMap();
  loadStatus();
  setInterval(()=>{if (!document.hidden) loadStatus();},60000);
  searchJourney();
}
initialize();
