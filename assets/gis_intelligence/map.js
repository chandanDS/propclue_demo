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
