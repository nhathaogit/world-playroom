'use strict';
(() => {
  const form = document.getElementById('creator');
  const errors = document.getElementById('errors');
  const submit = document.getElementById('publish');
  const draftStatus = document.getElementById('draft-status');
  const openingList = document.getElementById('opening-list');
  const metricList = document.getElementById('metric-list');
  const milestoneList = document.getElementById('milestone-list');
  const titleMetric = document.getElementById('title-metric');
  const draftKey = 'playroom.boyfriend-creator.v1';
  let portrait = '', previewUrl = '', imagePending = false, imageVersion = 0, busy = false, published = false, idEdited = false, draftTimer;
  const textFields = ['room_id','name','description','genre','origin','occupation','residence','appearance',
    'personality','habits','relationship','background','setting','starting_situation','voice','desire',
    'flaw','opening_knowledge','authority','initiative','change_conditions','ooc_guard','extra_rules','mode'];
  function node(tag, text, cls) { const el=document.createElement(tag); if(text!=null)el.textContent=text; if(cls)el.className=cls; return el; }
  function button(text, action) { const el=node('button',text,'secondary'); el.type='button'; el.addEventListener('click',action); return el; }
  function slug(text) { return text.toLowerCase().replaceAll('đ','d').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'').slice(0,54).replace(/-$/,''); }
  function labeledInput(label, value, options={}) { const wrap=node('label',label); const input=node(options.tag||'input'); if(input.tagName==='INPUT')input.type=options.type||'text'; input.value=value??''; input.required=true; if(options.maxLength)input.maxLength=options.maxLength; for(const key of ['min','max','step'])if(options[key]!=null)input[key]=options[key]; wrap.append(input); return [wrap,input]; }
  function changed() { document.getElementById('preview').hidden=true; clearTimeout(draftTimer); draftTimer=setTimeout(saveDraft,300); }
  function removeRow(row, list, minimum, after=()=>{}) { if(list.children.length<=minimum){errors.textContent='Cần giữ ít nhất một mục trong phần này.';return;} row.remove();after();changed(); }
  function addOpening(part={type:'narration',text:''}) {
    if(openingList.children.length>=12)return;
    const row=node('div',null,'row');const head=node('div',null,'row-head');const label=node('label','Loại đoạn');const select=node('select');
    for(const [value,text] of [['narration','Dẫn chuyện — ngôi thứ ba'],['dialogue','Lời thoại của nhân vật']]){const option=node('option',text);option.value=value;select.append(option);}select.value=part.type;
    const text=node('textarea');text.value=part.text;text.required=true;text.maxLength=1800;text.setAttribute('aria-label','Nội dung đoạn mở đầu');
    label.append(select);head.append(label,button('Bỏ đoạn',()=>removeRow(row,openingList,1)));row.append(head,text);openingList.append(row);
  }
  function updateMetricOptions() {
    const selected=Number(titleMetric.value||0);titleMetric.replaceChildren();
    [...metricList.children].forEach((row,index)=>{const option=node('option',row.querySelector('input').value||`Thuộc tính ${index+1}`);option.value=String(index);titleMetric.append(option);});
    titleMetric.value=String(Math.min(selected,metricList.children.length-1));
  }
  function addMetric(metric={label:'',initial:0}) {
    if(metricList.children.length>=8)return;
    const row=node('div',null,'row metric-row');const [label,input]=labeledInput('Tên thuộc tính',metric.label,{maxLength:60});const [score,number]=labeledInput('Điểm ban đầu',metric.initial,{type:'number',min:0,max:100,step:1});
    input.addEventListener('input',updateMetricOptions);row.append(label,score,button('Bỏ',()=>removeRow(row,metricList,1,()=>{titleMetric.value='0';updateMetricOptions();})));metricList.append(row);updateMetricOptions();
  }
  function addMilestone(milestone={at:0,title:'',meaning:''}) {
    if(milestoneList.children.length>=10)return;
    const row=node('div',null,'row');const head=node('div',null,'row-head');
    const [level]=labeledInput('Từ điểm',milestone.at,{type:'number',min:0,max:100,step:1});
    const [title]=labeledInput('Danh xưng',milestone.title,{maxLength:100});
    const [meaning]=labeledInput('Ý nghĩa / cảm nhận của nhân vật',milestone.meaning,{tag:'textarea',maxLength:600});
    head.append(level,button('Bỏ mốc',()=>removeRow(row,milestoneList,1)));row.append(head,title,meaning);milestoneList.append(row);
  }
  function payload() {
    const data={};for(const key of textFields)data[key]=form.elements.namedItem(key).value.trim();
    data.age=Number(form.elements.age.value);
    for(const key of ['height_cm','weight_kg'])data[key]=form.elements.namedItem(key).value?Number(form.elements.namedItem(key).value):null;
    data.opening=[...openingList.children].map(row=>({type:row.querySelector('select').value,text:row.querySelector('textarea').value.trim()}));
    data.metrics=[...metricList.children].map(row=>{const inputs=row.querySelectorAll('input');return {label:inputs[0].value.trim(),initial:Number(inputs[1].value)};});
    data.title_metric=Number(titleMetric.value);
    data.milestones=[...milestoneList.children].map(row=>{const inputs=row.querySelectorAll('input');return {at:Number(inputs[0].value),title:inputs[1].value.trim(),meaning:row.querySelector('textarea').value.trim()};});
    data.portrait=portrait;return data;
  }
  function saveDraft() {
    if(published)return;
    try { const data=payload();delete data.portrait;localStorage.setItem(draftKey,JSON.stringify(data));draftStatus.textContent='Đã giữ nháp nội dung chữ trong trình duyệt này. Ảnh không lưu trong bản nháp; cần chọn lại sau khi tải lại trang.'; }
    catch { draftStatus.textContent='Trình duyệt không lưu được nháp. Giữ trang mở cho tới khi tạo thành công.'; }
  }
  function restore() {
    let data=null;try{data=JSON.parse(localStorage.getItem(draftKey));}catch{/* Start blank if browser storage is unavailable or stale. */}
    if(!data||typeof data!=='object')return false;
    for(const key of [...textFields,'age','height_cm','weight_kg'])if(typeof data[key]==='string'||typeof data[key]==='number')form.elements.namedItem(key).value=data[key];
    for(const p of (Array.isArray(data.opening)?data.opening:[]).slice(0,12))if(p&&['narration','dialogue'].includes(p.type)&&typeof p.text==='string')addOpening(p);
    for(const m of (Array.isArray(data.metrics)?data.metrics:[]).slice(0,8))if(m&&typeof m.label==='string')addMetric(m);
    for(const m of (Array.isArray(data.milestones)?data.milestones:[]).slice(0,10))if(m&&typeof m.title==='string'&&typeof m.meaning==='string')addMilestone(m);
    titleMetric.value=String(data.title_metric||0);idEdited=Boolean(data.room_id);
    draftStatus.textContent='Đã khôi phục nháp nội dung chữ. Nếu đã có ảnh, hãy chọn lại ảnh trước khi tạo.';return true;
  }
  function showProblems(messages) { errors.replaceChildren();for(const message of messages)errors.append(node('p',message));errors.focus(); }
  function check(data) {
    const messages=[];const levels=data.milestones.map(m=>m.at);
    if(levels[0]!==0||levels.some((value,index)=>index>0&&value<=levels[index-1]))messages.push('Mốc danh xưng phải bắt đầu từ 0 và tăng dần, không trùng điểm.');
    if(new Set(data.metrics.map(m=>m.label.toLocaleLowerCase('vi'))).size!==data.metrics.length)messages.push('Tên thuộc tính quan hệ không được trùng.');
    if(new Set(data.milestones.map(m=>m.title.toLocaleLowerCase('vi'))).size!==data.milestones.length)messages.push('Tên danh xưng không được trùng.');
    if(imagePending)messages.push('Ảnh đang được đọc, vui lòng chờ một chút.');
    if(document.getElementById('portrait-error').textContent)messages.push('Hãy chọn lại ảnh hợp lệ, hoặc bỏ chọn ảnh.');
    const authored={...data};delete authored.portrait;if(new TextEncoder().encode(JSON.stringify(authored)).length>28000)messages.push('Hồ sơ vượt 28 KB. Rút gọn để dành context cho chat.');
    if(messages.length){showProblems(messages);return false;}return true;
  }
  function preview() {
    if(!form.reportValidity())return;const data=payload();if(!check(data))return;
    errors.replaceChildren();const content=document.getElementById('preview-content');content.replaceChildren();
    for(const [title,text] of [[data.name,`${data.age} tuổi · ${data.occupation}`],['Giới thiệu',data.description],['Tính cách',data.personality],['Giọng nói',data.voice],['Quan hệ ban đầu',data.relationship],['Trước cuộc gặp',data.background],['Tình huống mở đầu',data.starting_situation]])content.append(node('h3',title),node('p',text));
    content.append(node('h3','Tin nhắn đầu tiên'));for(const part of data.opening)content.append(node('p',part.text,part.type==='dialogue'?'bubble':''));
    const section=document.getElementById('preview');section.hidden=false;section.scrollIntoView({block:'start'});
  }
  form.elements.name.addEventListener('input',()=>{if(!idEdited)form.elements.room_id.value=slug(form.elements.name.value);});
  form.elements.room_id.addEventListener('input',()=>{idEdited=Boolean(form.elements.room_id.value);});
  form.addEventListener('input',changed);form.addEventListener('change',changed);
  document.getElementById('add-opening').onclick=()=>{addOpening();changed();};
  document.getElementById('add-metric').onclick=()=>{addMetric();changed();};
  document.getElementById('add-milestone').onclick=()=>{addMilestone();changed();};
  document.getElementById('preview-button').onclick=preview;
  function clearPortrait() {
    ++imageVersion;portrait='';imagePending=false;document.getElementById('portrait').value='';
    document.getElementById('portrait-error').textContent='';
    if(previewUrl){URL.revokeObjectURL(previewUrl);previewUrl='';}
    document.getElementById('portrait-preview').src='/static/thumbnails/companion-placeholder.svg';
  }
  document.getElementById('clear-portrait').onclick=()=>{clearPortrait();changed();};
  document.getElementById('portrait').addEventListener('change',async event=>{
    const version=++imageVersion;const file=event.target.files[0];const problem=document.getElementById('portrait-error');portrait='';imagePending=false;problem.textContent='';
    if(previewUrl){URL.revokeObjectURL(previewUrl);previewUrl='';}document.getElementById('portrait-preview').src='/static/thumbnails/companion-placeholder.svg';
    if(!file)return;
    if(!['image/png','image/jpeg','image/webp'].includes(file.type)||file.size>2000000){problem.textContent='Chọn ảnh PNG/JPEG/WebP không quá 2 MB.';return;}
    imagePending=true;
    const reader=new FileReader();reader.onload=()=>{if(version!==imageVersion)return;portrait=String(reader.result);previewUrl=URL.createObjectURL(file);document.getElementById('portrait-preview').src=previewUrl;imagePending=false;};
    reader.onerror=()=>{if(version!==imageVersion)return;problem.textContent='Không đọc được ảnh; hãy chọn lại.';imagePending=false;};reader.readAsDataURL(file);
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(busy||published)return;const data=payload();if(!check(data))return;
    busy=true;const controls=[...form.elements].filter(element=>!element.disabled);for(const element of controls)element.disabled=true;submit.textContent='Đang kiểm tra & lưu…';errors.replaceChildren();saveDraft();
    try {
      const response=await fetch('/api/companions',{method:'POST',headers:{'Content-Type':'application/json','X-Companion-Editor':'1'},body:JSON.stringify(data)});
      const result=await response.json().catch(()=>({detail:'Server không trả dữ liệu hợp lệ.'}));
      if(!response.ok){const detail=result.detail;const labels={room_id:'Mã nhân vật',name:'Tên nhân vật',age:'Tuổi',opening:'Tin nhắn mở đầu',metrics:'Thuộc tính quan hệ',milestones:'Mốc danh xưng',portrait:'Ảnh',title_metric:'Thuộc tính xét danh xưng'};const messages=Array.isArray(detail)?detail.map(e=>`${(e.loc||[]).map(k=>labels[k]||k).join(' → ')}: ${e.msg}`):[typeof detail==='string'?detail:'Không lưu được nhân vật.'];showProblems(messages);return;}
      published=true;clearTimeout(draftTimer);try{localStorage.removeItem(draftKey);}catch{}
      draftStatus.textContent='Đã tạo trên máy chạy server. Bản nháp trình duyệt đã được xóa.';
      const box=document.getElementById('result');box.append(node('p',`Đã tạo ${result.title}. Nhân vật có save riêng, chưa bắt đầu chat.`));
      for(const [label,url] of [['Mở hồ sơ & nhập persona',result.profile_url],['Về chợ Boyfriend','/boyfriend']]){const link=node('a',label);link.href=url;box.append(link);}
      submit.textContent='Đã tạo nhân vật';for(const element of form.elements)element.disabled=true;box.scrollIntoView({block:'center'});
    } catch { showProblems(['Không kết nối được server hoặc phản hồi bị gián đoạn. Nháp vẫn còn. Kiểm tra chợ trước khi thử lại; nếu mã đã tồn tại, backend sẽ không ghi đè.']); }
    finally {busy=false;if(!published){for(const element of controls)element.disabled=false;submit.textContent='Tạo & đưa lên chợ';}}
  });
  restore();if(!openingList.children.length){addOpening();addOpening({type:'dialogue',text:''});}
  if(!metricList.children.length)for(const label of ['Tình cảm','Tin tưởng','Gắn bó','Tôn trọng'])addMetric({label,initial:0});
  if(!milestoneList.children.length)for(const item of [
    {at:0,title:'Người được chú ý',meaning:'Anh chú ý đến Player; chưa xác nhận tình cảm hai chiều.'},
    {at:25,title:'Người muốn tìm hiểu',meaning:'Anh muốn hiểu Player hơn, tôn trọng khoảng cách người chơi chọn.'},
    {at:55,title:'Sự hiện diện được trân trọng',meaning:'Anh trân trọng sự gắn bó qua tương tác; chưa tự xác lập quan hệ.'},
    {at:85,title:'Tình cảm sâu sắc từ phía anh',meaning:'Mô tả cảm nhận của anh, không buộc Player đáp lại.'}])addMilestone(item);
})();
