const roomId = decodeURIComponent(location.pathname.split('/')[2] || '');
const headers = {'X-Playroom-ID':roomId, 'Content-Type':'application/json'};
const el = id => document.getElementById(id);
let characters = [];
function mode(){return document.querySelector('input[name="mode"]:checked').value;}
function renderProfile(){
  const item = characters.find(c=>c.id === el('character').value);
  el('profile').textContent = item ? [item.name,item.guild,item.magic,item.personality,item.limits].filter(Boolean).join(' · ') : 'Không có nhân vật phù hợp.';
}
function renderCharacters(){
  const previous = el('character').value;
  const query = el('search').value.toLocaleLowerCase('vi');
  el('character').replaceChildren();
  for(const item of characters.filter(c=>[c.name,c.guild,c.magic].join(' ').toLocaleLowerCase('vi').includes(query))){
    const option=document.createElement('option');option.value=item.id;option.textContent=item.name+(item.guild?' · '+item.guild:'');el('character').append(option);
  }
  if([...el('character').options].some(o=>o.value===previous))el('character').value=previous;
  renderProfile();
}
document.querySelectorAll('input[name="mode"]').forEach(input=>input.addEventListener('change',()=>{
  el('original').hidden=mode()!=='original';el('canon').hidden=mode()!=='canon';
}));
el('search').addEventListener('input',renderCharacters);
el('character').addEventListener('change',renderProfile);
el('create').addEventListener('submit',async event=>{
  event.preventDefault();el('error').textContent='';
  if(mode()==='original'&&!el('name').value.trim()){el('error').textContent='Hãy đặt tên nhân vật.';return;}
  if(mode()==='canon'&&!el('character').value){el('error').textContent='Hãy chọn nhân vật.';return;}
  el('start').disabled=true;
  try{
    const response=await fetch('/api/play-sessions',{method:'POST',headers,body:JSON.stringify({mode:mode(),name:el('name').value,gender:el('gender').value,persona:el('persona').value,character_id:el('character').value})});
    const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'Không tạo được phiên.');
    location.href=data.play_url;
  }catch(error){el('error').textContent=error.message;el('start').disabled=false;}
});
async function load(){
  try{
    const response=await fetch('/api/play-sessions',{headers});const data=await response.json();
    if(!response.ok||!data.enabled)throw Error('Room chưa hỗ trợ lựa chọn vai.');
    characters=data.characters;renderCharacters();el('sessions').replaceChildren();
    for(const session of data.sessions){
      const link=document.createElement('a');link.href='/play/'+encodeURIComponent(roomId)+(session.id==='legacy'?'':'?session='+encodeURIComponent(session.id));
      link.textContent=session.name+(session.mode==='canon'?' · Vai có sẵn':session.mode==='original'?' · Nhân vật tự tạo':'')+(session.created_at?' · '+new Date(session.created_at).toLocaleString('vi-VN'):'');el('sessions').append(link);
    }
    el('start').disabled=false;
  }catch(error){el('error').textContent=error.message;}
}
load();
