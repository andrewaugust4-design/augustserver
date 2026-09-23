// Quest rendering helpers shared by the Quest Browser (index.html) and the
// guide page (guide.html). Both pages resolve relative URLs against /wow/quests/.

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function api(path, body) {
  const res = await fetch('../api/' + path, body === undefined ? {} :
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) {
    const detail = (await res.json().catch(() => ({}))).detail;
    // FastAPI validation errors are a list of {msg}; everything else is a string.
    throw new Error(Array.isArray(detail) ? detail.map(d => d.msg.replace(/^Value error, /, '')).join('; ') : detail || res.statusText);
  }
  return res.json();
}

const FACTION_LABEL = { alliance: 'Alliance', horde: 'Horde', both: 'Both' };
const QUALITY_COLORS = {0:'#9d9d9d',1:'#ffffff',2:'#1eff00',3:'#0070dd',4:'#a335ee',5:'#ff8000',6:'#e6cc80',7:'#00ccff'};
const PLACEHOLDER_ICON = '../icon-placeholder.svg';

// Reward item icons come from the suite's self-hosted icon cache (/api/icon).
function iconImg(id, quality) {
  const q = QUALITY_COLORS[quality];
  return `<img class="icon" src="../api/icon/${id}.jpg" alt="" loading="lazy" style="${q ? '--q:' + q : ''}" ` +
         `onerror="this.onerror=null;this.src='${PLACEHOLDER_ICON}'">`;
}

function money(copper) {
  const g = Math.floor(copper / 10000), s = Math.floor(copper / 100) % 100, c = copper % 100;
  return `<span class="money">${g ? `${g}<span class="g">g</span> ` : ''}${s ? `${s}<span class="s">s</span> ` : ''}${c || (!g && !s) ? `${c}<span class="c">c</span>` : ''}</span>`;
}

function who(ends) {
  if (!ends) return '';
  const parts = [
    ...(ends.npcs || []).map(n => esc(n.name) + (n.zone ? ` <span class="sub">(${esc(n.zone)})</span>` : '')),
    ...(ends.objects || []).map(o => esc(o.name)),
    ...(ends.items || []).map(i => `<span style="color:${QUALITY_COLORS[i.quality] || '#fff'}">[${esc(i.name)}]</span>`),
  ];
  return [...new Set(parts)].join(', ');
}

function rewardsHtml(r) {
  if (!r) return '<p class="pending">Rewards not yet revealed — server-side until seen in-world.</p>';
  const kinds = { choice: 'Choose one', guaranteed: 'You will receive', reward: 'Reward' };
  const groups = ['guaranteed', 'choice', 'reward'].map(kind => {
    const items = r.items.filter(i => i.kind === kind);
    if (!items.length) return '';
    return `<div class="kind" style="margin-top:0.35rem">${kinds[kind]}</div>` + items.map(i => `
      <div class="reward">${iconImg(i.id, i.quality)}
        <span style="color:${QUALITY_COLORS[i.quality] || '#fff'}">${esc(i.name)}</span>${i.count > 1 ? ` <span class="sub">×${i.count}</span>` : ''}</div>`).join('');
  }).join('');
  const extras = [
    r.money ? `Money: ${money(r.money)}` : null,
    r.xp ? `XP: <span class="money">${r.xp.toLocaleString()}</span>` : null,
    ...r.reputation.map(x => `${esc(x.faction)}: <span class="money">${x.value > 0 ? '+' : ''}${x.value}</span> reputation`),
  ].filter(Boolean).map(x => `<div>${x}</div>`).join('');
  return groups + extras || '<p class="sub">No rewards listed.</p>';
}

function objectivesHtml(q) {
  if (!q.objectives_text.length && !q.objectives.length) return '';
  return `<h3>Objectives</h3>
    ${q.objectives_text.filter(Boolean).map(t => `<p>${esc(t)}</p>`).join('')}
    ${q.objectives.length ? `<ul style="margin:0.3rem 0 0 1.1rem">${q.objectives.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}`;
}
