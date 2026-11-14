"""Isolated third GIS map with self-contained frontend assets."""
import streamlit as st

_HTML = r"""
<section class="gis-shell" aria-label="GIS intelligence map">
  <div class="gis-map" aria-label="Zoomable HSR map"></div>
  <header class="gis-toolbar">
    <div class="gis-brand"><span class="gis-logo">⌘</span><div>GIS intelligence<small>PROPCLUE / SPATIAL EXPLORER</small></div></div>
    <form class="gis-search" role="search"><span aria-hidden="true">⌕</span><input aria-label="Search a place, locality, survey number or project" placeholder="Search a place, locality, survey number or project" autocomplete="off"><button type="submit">Search</button><div class="gis-results" hidden></div></form>
    <button class="gis-fullscreen" type="button" aria-label="Toggle fullscreen map">⛶ <span>Full screen</span></button>
  </header>
  <div class="gis-demo">DEMO GEOMETRY <span>Illustrative boundaries & parcels · not cadastral records</span></div>
  <div class="gis-tools"><button class="gis-plus" aria-label="Zoom in">+</button><button class="gis-minus" aria-label="Zoom out">−</button><button class="gis-home" aria-label="Return to HSR Layout">⌂</button></div>
  <aside class="gis-pane" aria-label="Geographic and property details"><div class="gis-panel-content"></div></aside>
  <div class="gis-legend"><b>PARCEL TYPE</b><span><i style="background:#8db694"></i>Open land</span><span><i style="background:#e5ba76"></i>Building</span><span><i style="background:#8cb3c4"></i>Apartment</span></div>
  <footer class="gis-bottom"><nav class="gis-crumbs" aria-label="Geographic drill-down"></nav><div class="gis-status" aria-live="polite"></div></footer>
  <div class="gis-error" role="status" hidden></div>
</section>

"""

_CSS = r"""
.gis-shell{position:relative;height:760px;min-height:640px;width:100%;overflow:hidden;border:1px solid #d8e2dd;border-radius:18px;background:#e8ece5;color:#20382f;font:14px/1.5 'Segoe UI',Arial,sans-serif;isolation:isolate}
.gis-shell *{box-sizing:border-box}.gis-shell button,.gis-shell input{font:inherit}.gis-shell button{cursor:pointer}.gis-shell button:focus-visible,.gis-shell input:focus-visible{outline:3px solid #158278;outline-offset:2px}.gis-map{position:absolute!important;inset:0;z-index:0;background:#e7ece2}.gis-toolbar{position:absolute;z-index:600;top:18px;left:20px;right:20px;display:flex;align-items:center;gap:16px;pointer-events:none}.gis-toolbar>*{pointer-events:auto}.gis-brand{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:700;background:#fff;padding:11px 16px;border-radius:12px;box-shadow:0 3px 16px #19372d18}.gis-logo{font-size:29px;color:#167566}.gis-brand small{display:block;font-size:8px;letter-spacing:1.7px;color:#78877f;font-weight:600}.gis-search{position:relative;display:flex;align-items:center;gap:12px;background:white;padding:8px 10px 8px 17px;border:1px solid #e3e9e5;border-radius:12px;width:min(530px,48%);box-shadow:0 4px 22px #19372d1a}.gis-search>span{font-size:26px;color:#5a7566}.gis-search input{width:100%;min-width:0;border:0;outline:0;background:white;color:#20382f;font-size:13px}.gis-search button{border:0;background:#176657;color:white;border-radius:7px;padding:9px 12px;font-size:12px}.gis-results{position:absolute;top:calc(100% + 8px);left:0;right:0;background:white;border:1px solid #dce5df;border-radius:10px;padding:6px;box-shadow:0 9px 25px #20382f25;max-height:280px;overflow:auto}.gis-results button{display:block;text-align:left;width:100%;background:white;color:#20382f;padding:10px}.gis-results button:hover{background:#edf5ef}.gis-fullscreen{margin-left:auto;border:1px solid #dce5df;border-radius:10px;padding:11px 13px;background:#fff;font-size:18px;box-shadow:0 3px 16px #19372d18;white-space:nowrap}.gis-fullscreen span{font-size:12px}.gis-demo{position:absolute;left:22px;top:99px;z-index:450;background:#fff9eb;border:1px solid #ead7ad;border-radius:7px;padding:6px 10px;color:#89601a;font-size:10px;letter-spacing:.6px;font-weight:700}.gis-demo span{font-weight:400;letter-spacing:0;margin-left:8px}.gis-pane{position:absolute;right:20px;top:99px;bottom:72px;z-index:500;width:330px;background:#fffffff7;border:1px solid #e0e7e1;border-radius:13px;box-shadow:0 6px 30px #19372d16;overflow-y:auto;overscroll-behavior:contain}.gis-panel-content{padding:21px}.gis-eyebrow{font-size:10px;color:#267d6d;font-weight:800;letter-spacing:1.5px;text-transform:uppercase}.gis-pane h2{font-size:24px;line-height:1.2;margin:9px 0 6px;letter-spacing:-.7px}.gis-subtitle{font-size:12px;color:#77877d;margin-bottom:14px}.gis-pin{border:1px solid #d2e2d8;border-radius:7px;background:#eef6f0;color:#286d59;font-size:11px;padding:7px 10px}.gis-section{font-size:10px;letter-spacing:1.5px;font-weight:750;color:#7d8b81;margin:23px 0 6px;text-transform:uppercase}.gis-row{display:flex;justify-content:space-between;gap:15px;align-items:baseline;padding:9px 0;border-bottom:1px solid #edf0ec;font-size:12px}.gis-row>span:first-child{color:#65776c}.gis-row>span:last-child{text-align:right;max-width:58%;overflow-wrap:anywhere;font-weight:600}.gis-missing{color:#c87616}.gis-note{font-size:11px;color:#89958c;margin:18px 0 0;padding:10px 0;border-top:1px solid #e8ede8}.gis-tools{position:absolute;z-index:450;left:22px;top:154px;display:flex;flex-direction:column;box-shadow:0 2px 15px #1d3e2920;border-radius:10px;overflow:hidden}.gis-tools button{border:0;border-bottom:1px solid #e0e8df;background:white;width:40px;height:40px;font-size:23px;color:#365744}.gis-tools button:hover{background:#eaf5ed}.gis-legend{position:absolute;z-index:450;bottom:90px;left:22px;background:#fffffff0;border:1px solid #dce5df;padding:12px 15px;border-radius:10px;display:flex;gap:14px;font-size:11px;align-items:center}.gis-legend b{font-size:9px;letter-spacing:1px;color:#6e8174}.gis-legend span{display:flex;align-items:center;gap:5px}.gis-legend i{width:10px;height:10px;display:inline-block;border-radius:2px}.gis-bottom{position:absolute;z-index:500;left:22px;right:22px;bottom:22px;display:flex;align-items:center;justify-content:space-between;gap:12px}.gis-crumbs{display:flex;flex-wrap:wrap;gap:5px;background:#fffffff0;padding:7px;border-radius:9px;box-shadow:0 3px 18px #19372d15}.gis-crumbs button{font-size:11px;border:0;background:transparent;color:#5c7162;padding:6px 9px;border-radius:5px}.gis-crumbs button.active{background:#1a6b58;color:white}.gis-status{background:#fffffff0;font-size:10px;color:#5b7564;border-radius:7px;padding:10px;white-space:nowrap}.gis-error{position:absolute;z-index:700;left:22px;bottom:145px;background:#fff4df;border:1px solid #e2be76;padding:10px;border-radius:8px;max-width:450px;font-size:12px}.gis-shell:fullscreen{height:100vh;width:100vw;border:0;border-radius:0}.gis-shell.gis-expanded{position:fixed;inset:0;width:100vw;height:100vh;z-index:99999;border-radius:0}
.gis-shell [hidden]{display:none!important}
.gis-shell .gis-pin{margin:0 6px 10px 0}.gis-shell path.leaflet-interactive:focus{outline:0;stroke:#126b57;stroke-width:4}
/* Core positioning is inline so Leaflet also renders correctly inside Shadow DOM. */
.gis-shell .leaflet-container{overflow:hidden;outline:0}.gis-shell .leaflet-pane,.gis-shell .leaflet-tile,.gis-shell .leaflet-marker-icon,.gis-shell .leaflet-marker-shadow,.gis-shell .leaflet-tile-container,.gis-shell .leaflet-pane>svg,.gis-shell .leaflet-pane>canvas,.gis-shell .leaflet-zoom-box{position:absolute;left:0;top:0}.gis-shell img.leaflet-tile{max-width:none!important;max-height:none!important;padding:0}.gis-shell .leaflet-pane>svg{max-width:none!important;max-height:none!important}.gis-shell .leaflet-control-container{position:absolute;inset:0;pointer-events:none}.gis-shell .leaflet-bottom{position:absolute;bottom:0;z-index:1000}.gis-shell .leaflet-right{right:0}.gis-shell .leaflet-control{pointer-events:auto}.gis-shell .leaflet-control-attribution{background:#ffffffe0;font:9px/1.5 Arial;padding:0 5px}.gis-shell .leaflet-interactive{cursor:pointer}.gis-shell .leaflet-tooltip{position:absolute;padding:5px 9px;background:white;border:1px solid #bbcfc1;border-radius:5px;font-size:11px;white-space:nowrap;pointer-events:none;color:#234f3b;box-shadow:0 2px 9px #20382f18}
.gis-shell .leaflet-map-pane{z-index:0}.gis-shell .leaflet-tile-pane{z-index:200}.gis-shell .leaflet-overlay-pane{z-index:400}.gis-shell .leaflet-shadow-pane{z-index:500}.gis-shell .leaflet-marker-pane{z-index:600}.gis-shell .leaflet-tooltip-pane{z-index:650}.gis-shell .leaflet-popup-pane{z-index:700}.gis-shell .leaflet-pane>svg{pointer-events:none}.gis-shell .leaflet-pane>svg path.leaflet-interactive{pointer-events:auto}.gis-shell .leaflet-zoom-animated{transform-origin:0 0}
@media(max-width:900px){.gis-brand{display:none}.gis-search{width:calc(100% - 70px)}.gis-fullscreen span{display:none}.gis-pane{width:285px}.gis-demo span{display:none}.gis-status{display:none}.gis-legend{gap:8px;padding:9px}.gis-legend b{display:none}}
@media(max-width:600px){.gis-shell{height:780px}.gis-pane{top:auto;left:12px;right:12px;bottom:75px;width:auto;height:275px}.gis-toolbar{left:12px;right:12px;gap:8px}.gis-search input{font-size:11px}.gis-search{padding-left:9px;gap:5px}.gis-search button{padding:9px}.gis-legend{bottom:362px;left:12px}.gis-bottom{left:12px;right:12px;bottom:20px}.gis-crumbs{gap:0;padding:3px}.gis-crumbs button{font-size:9px;padding:5px}.gis-demo{left:12px}.gis-panel-content{padding:16px}}

"""

_JS = r"""
export default function(component) {
  const root = component.parentElement;
  const shell = root.querySelector('.gis-shell');
  if (!shell || shell.dataset.mounted) return;
  shell.dataset.mounted = 'true';
  const $ = s => shell.querySelector(s);
  const error = message => { $('.gis-error').textContent = message; $('.gis-error').hidden = false; };
  let disposed = false, map, observer;
  const center = [12.9121, 77.6446];
  const levels = ['State', 'District', 'City', 'Zone', 'Area', 'Project', 'Parcel', 'Building'];
  const zooms = [6, 9, 11, 13, 15, 17, 18, 20];
  const names = ['Karnataka', 'Bengaluru Urban', 'Bengaluru', 'Bommanahalli', 'HSR Layout', 'HSR sample project'];
  const bounds = [
    [[11.6,74.0],[18.5,78.6]], [[12.65,77.32],[13.24,77.86]],
    [[12.79,77.43],[13.14,77.79]], [[12.86,77.60],[12.94,77.69]],
    [[12.899,77.630],[12.925,77.658]], [[12.9098,77.6419],[12.9145,77.6473]]
  ];
  const records = [];
  function record(id, name, level, box, values = {}) {
    return { id, name, level, bounds:box, values:{'Name':name, 'Data source':'Illustrative demo', ...values} };
  }
  for (let i=0;i<6;i++) records.push(record('level-'+i,names[i],i,bounds[i],{'Parent geography':i ? names[i-1] : 'India'}));
  const types = ['Open land','Building','Apartment'];
  for (let row=0;row<4;row++) for (let col=0;col<5;col++) {
    const i=row*5+col, lat=12.91005+row*.00106, lon=77.64215+col*.00098;
    const box=[[lat,lon],[lat+.00088,lon+.00079]];
    const type=types[i%3], survey=(36+row)+'/'+(col+1);
    const parcel=record('parcel-'+i,'Sy. No. '+survey,6,box,{
      'Survey number':survey+' · demo', 'Parcel type':type, 'Locality':'HSR Layout',
      'State':'Karnataka', 'District':'Bengaluru Urban', 'City':'Bengaluru',
      'Zone':'Bommanahalli', 'Project name':type==='Open land'?null:'HSR sample project',
      'Property type':type==='Open land'?'Open land':type==='Apartment'?'Apartment building':'Independent building'
    });
    records.push(parcel);
    if(type!=='Open land') records.push(record('building-'+i,(type==='Apartment'?'Apartment ':'Building ')+(i+1),7,
      [[lat+.00016,lon+.00014],[lat+.00069,lon+.00063]],{
        ...parcel.values, 'Name':(type==='Apartment'?'Apartment ':'Building ')+(i+1),
        'Parcel reference':survey+' · demo', 'Floors':String(2+i%5)+' · demo',
        'Units':type==='Apartment'?String(8+i*2)+' · demo':'1 · demo', 'Year built':null
      }));
  }
  const schemas = {
    geography:{'Identity':['Name','Geographic code','Parent geography','Official area'],
      'Administration':['State','District','City','Zone','Ward','Governing authority'],
      'Demographics':['Population','Households','Population density'],
      'Market':['Property count','Median sale price','Median rent','Price growth'],
      'Infrastructure':['Nearest metro','Schools','Hospitals','Water supply','Road access'],
      'Evidence':['Data source','Source updated','Boundary verification']},
    project:{'Identity':['Name','Project name','Developer','RERA number','Project status','Official area'],
      'Location':['State','District','City','Zone','Locality','Address'],
      'Inventory':['Property type','Buildings','Floors','Units','Available units','Possession date'],
      'Approvals':['Plan approval','Occupancy certificate','Completion certificate','Bank approval'],
      'Market':['Asking price','Price per sq ft','Monthly rent'],
      'Evidence':['Data source','Source updated','Boundary verification']},
    property:{'Identity':['Name','Survey number','ULPIN','Village','Parcel reference','Official area'],
      'Land':['Parcel type','Land category','Government land type','Land use','Zoning','Distance to nearest lake','Distance to nearest drain'],
      'Built form':['Property type','Building name','Floors','Units','Year built','Built-up area'],
      'Project':['Project name','Developer','RERA number','Project status'],
      'Location':['State','District','City','Zone','Locality','Ward','Address'],
      'Ownership & records':['Owner name','Title status','Khata number','Encumbrance certificate','Property tax status'],
      'Approvals':['Plan approval','Occupancy certificate','Completion certificate'],
      'Market':['Asking price','Price per sq ft','Monthly rent','Last transaction'],
      'Evidence':['Data source','Source updated','Boundary verification']}
  };
  let level=6, pinned=null, hovered=null, layers=[], selected=records.find(r=>r.id==='parcel-7');
  const text=(parent,tag,cls,value)=>{const el=document.createElement(tag);el.className=cls;el.textContent=value;parent.appendChild(el);return el;};
  function childOf(r) {
    if(r.level<6)return records.find(item=>item.level===r.level+1);
    if(r.level===6)return records.find(item=>item.id===r.id.replace('parcel-','building-'));
    return null;
  }
  const exploreLabels=['Explore districts','Explore cities','Explore zones','Explore localities','Explore projects','Explore parcels','Open building details'];
  function renderPanel(r) {
    selected=r; const panel=$('.gis-panel-content'); panel.replaceChildren();
    text(panel,'div','gis-eyebrow',levels[r.level]+' / '+(r.values['Parcel type']||'Spatial intelligence'));
    text(panel,'h2','',r.name);
    text(panel,'div','gis-subtitle',r.level>=5?'HSR Layout · Bengaluru · Karnataka':'India · '+r.name);
    const child=childOf(r);
    if(child) {
      const explore=text(panel,'button','gis-pin',exploreLabels[r.level]+' →');
      explore.type='button';explore.onclick=()=>{if(map)jump(child);};
    }
    if(r.level>=6) {
      const pin=text(panel,'button','gis-pin',pinned?'Pinned · click to unpin':'Pin property details');
      pin.type='button';pin.onclick=()=>togglePin(r);
    }
    text(panel,'div','gis-subtitle',r.level<6?'Click a highlighted boundary to explore the next level.':child?'Click a parcel to pin it. Open building details to see its footprint.':'Click a property to pin its details.');
    const schema=r.level>=6?schemas.property:r.level===5?schemas.project:schemas.geography;
    let available=0,total=0;
    for(const [section,fields] of Object.entries(schema)) {
      text(panel,'div','gis-section',section);
      for(const field of fields) {
        const value=r.values[field], has=value!==null&&value!==undefined&&value!=='';
        total++;if(has)available++;
        const row=text(panel,'div','gis-row','');
        text(row,'span','',field);text(row,'span',has?'':'gis-missing',has?value:'Not available');
      }
    }
    text(panel,'div','gis-note',available+' of '+total+' fields populated. All shapes and sample property values are illustrative; official areas, ownership and approvals are not connected.');
  }
  function levelForZoom(z) {let i=0;while(i<zooms.length-1&&z>=zooms[i+1])i++;return i;}
  function color(r){return r.level>=6?({'Open land':'#8db694','Building':'#e5ba76','Apartment':'#8cb3c4'}[r.values['Parcel type']]||'#e5ba76'):'#9ab9a1';}
  function style(r,active=false){return {color:active?'#126b57':'#668b74',weight:active?3:1.5,fillColor:color(r),fillOpacity:active?.7:r.level<5?.12:.45};}
  function jump(r) {pinned=null;hovered=null;selected=r;map.setView([(r.bounds[0][0]+r.bounds[1][0])/2,(r.bounds[0][1]+r.bounds[1][1])/2],zooms[r.level],{animate:false});draw();}
  function togglePin(r) {
    pinned=pinned?.id===r.id?null:r;renderPanel(r);
    for(const item of layers)item.shape.setStyle(style(item.record,pinned?.id===item.record.id));
    crumbs();
  }
  function crumbs() {
    const nav=$('.gis-crumbs');nav.replaceChildren();
    for(let i=0;i<=Math.min(level,5);i++) {const b=text(nav,'button',i===level?'active':'',names[i]);b.type='button';b.onclick=()=>jump(records[i]);}
    if(level>=6){const b=text(nav,'button','active',levels[level]);b.type='button';b.onclick=()=>jump(selected.level===level?selected:records.find(r=>r.level===level));}
    $('.gis-status').textContent=levels[level]+' layer · zoom '+map.getZoom()+' · '+(pinned?'Pinned':level<6?'Click to drill down':'Click to pin details');
  }
  function draw() {
    if(!map)return;
    level=levelForZoom(map.getZoom());
    for(const layer of layers)layer.remove();layers=[];
    const visible=records.filter(r=>r.level===level||(level===7&&r.level===6&&r.values['Parcel type']==='Open land'));
    for(const r of visible) {
      const shape=window.L.rectangle(r.bounds,style(r,pinned?.id===r.id)).addTo(map);
      shape.bindTooltip(r.name+(r.level>=6?' · '+r.values['Parcel type']:''),{sticky:true,direction:'top'});
      shape.on('mouseover',()=>{hovered=r;shape.setStyle(style(r,true));shape.bringToFront();if(!pinned)renderPanel(r);});
      shape.on('mouseout',()=>{hovered=null;shape.setStyle(style(r,pinned?.id===r.id));});
      const activate=()=>{const next=childOf(r);if(r.level<6&&next)jump(next);else togglePin(r);};
      shape.on('click',e=>{window.L.DomEvent.stopPropagation(e);activate();});
      const element=shape.getElement();
      element.setAttribute('tabindex','0');element.setAttribute('role','button');
      element.setAttribute('aria-label',r.name+' · '+(r.level<6?exploreLabels[r.level]:'Pin property details'));
      element.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();e.stopPropagation();activate();}});
      layers.push({remove:()=>shape.remove(),shape,record:r});
    }
    if(pinned&&pinned.level!==level)pinned=null;
    if(!pinned)renderPanel(visible.find(r=>r.id===selected.id)||visible[0]);
    crumbs();$('.gis-legend').hidden=level<6;
  }
  function search(query) {
    const q=query.trim().toLowerCase(), results=$('.gis-results');results.replaceChildren();results.hidden=!q;
    if(!q)return [];
    const matches=records.filter(r=>(r.name+' '+r.values['Survey number']+' '+r.values['Parcel type']+' '+(r.level>=5?'hsr ': '')).toLowerCase().includes(q)).slice(0,8);
    if(!matches.length)text(results,'div','','No matching demo record. Try HSR, Bengaluru, 37/3 or Apartment.');
    for(const r of matches){const b=text(results,'button','',r.name+' · '+levels[r.level]);b.type='button';b.onclick=()=>{jump(r);results.hidden=true;$('.gis-search input').value=r.name;};}
    return matches;
  }
  $('.gis-search input').oninput=e=>search(e.target.value);
  $('.gis-search input').onkeydown=e=>{if(e.key==='Escape')$('.gis-results').hidden=true;};
  $('.gis-search').onsubmit=e=>{e.preventDefault();const matches=search($('.gis-search input').value);if(matches.length&&map){jump(matches[0]);$('.gis-results').hidden=true;}};
  $('.gis-plus').onclick=()=>map?.zoomIn();$('.gis-minus').onclick=()=>map?.zoomOut();
  $('.gis-home').onclick=()=>{if(map){pinned=null;map.setView(center,18);draw();}};
  $('.gis-fullscreen').onclick=async()=>{
    if(document.fullscreenElement){await document.exitFullscreen();}
    else if(shell.classList.contains('gis-expanded'))shell.classList.remove('gis-expanded');
    else{try{await shell.requestFullscreen();}catch{shell.classList.add('gis-expanded');}}
    setTimeout(()=>map?.invalidateSize(),100);
  };
  function onFullscreen(){setTimeout(()=>map?.invalidateSize(),100);}
  document.addEventListener('fullscreenchange',onFullscreen);
  function onEscape(e){if(e.key==='Escape'){shell.classList.remove('gis-expanded');map?.invalidateSize();}}
  document.addEventListener('keydown',onEscape);
  renderPanel(selected);
  async function start() {
    try {
      if(!window.L) {
        // Reuse the global loader without sharing any map or selection state.
        window.__propclueGisLeaflet ||= new Promise((resolve,reject)=>{
          const script=document.createElement('script');script.src='https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';script.onload=resolve;script.onerror=()=>reject(new Error('Map library unavailable'));document.head.appendChild(script);
        });
        await window.__propclueGisLeaflet;
      }
      if(disposed)return;
      map=window.L.map($('.gis-map'),{zoomControl:false,scrollWheelZoom:true,doubleClickZoom:false,minZoom:5,maxZoom:21}).setView(center,18);
      window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxNativeZoom:19,maxZoom:21,attribution:'© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'}).addTo(map).on('tileerror',()=>error('Street tiles unavailable. Demo boundaries, search and property details still work.'));
      map.on('zoomend',()=>{hovered=null;draw();});
      map.on('click',()=>{if(pinned){pinned=null;draw();}});
      observer=new ResizeObserver(()=>map.invalidateSize());observer.observe(shell);
      draw();
    } catch(e){error('Interactive map could not load. Check the connection and reload. The sample property fields remain available.');window.__propclueGisLeaflet=null;}
  }
  start();
  return ()=>{disposed=true;observer?.disconnect();map?.remove();document.removeEventListener('fullscreenchange',onFullscreen);document.removeEventListener('keydown',onEscape);delete shell.dataset.mounted;};
}

"""

_MAP = st.components.v2.component(
    "propclue_gis_intelligence_v1", html=_HTML, css=_CSS, js=_JS,
)


def render_gis_intelligence() -> None:
    """Render only the third map using isolated selection state."""
    _MAP(key="gis-intelligence-third-map", height=780, width="stretch")
