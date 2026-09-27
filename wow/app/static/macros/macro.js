// Macro Builder: the syntax model. No DOM here, so it can be tested on its own.
//
// Hand-encoded from the modern (retail-derived) macro system that Forever's
// client uses, minus the retail-only parts that don't exist in a level-60
// classic world. Nothing in this file is datamined; the spell lists come
// from /api/macros/<class>.
//
// A macro config (what the URL stores) is:
//   { sh: 0|1|2,              // 0 = no meta line, 1 = #showtooltip, 2 = #show
//     shn: Arg|null,          // optional spell/item named on that line
//     l: [Line] }
//   Line   = { c: command, b: [Branch] }
//   Branch = { g: [Bracket],  // OR'd bracket groups: [a][b] X. [] = empty list, no brackets
//              a: Arg,        // /cast, /use, /cancelaura, /target … argument
//              q: [Arg],      // /castsequence, /castrandom spell list
//              rs: Reset }    // /castsequence reset= options
//   Bracket = { u, r, e, d, m, c, s, st, mt, ch, b, sp, pt, gr, io, sw, x }  (see BRACKET_FIELDS)
//   Arg    = { n: name, r: rank (0 = highest), t: toggle "!" }
//   Reset  = { s: seconds, t: target, c: combat, m: 'shift'|'ctrl'|'alt'|'' }
(function (root) {
  'use strict';

  const MAX_CHARS = 255;

  // ── commands ──────────────────────────────────────────────────────────
  // arg: what follows the conditionals. spell = spell (or item) name, item =
  // item name or equipment slot number, seq = castsequence list, list =
  // castrandom list, aura = buff name, text = optional free text, none.
  const COMMANDS = [
    { c: 'cast', arg: 'spell', group: 'Cast', desc: 'Cast a spell (or use an item by name)' },
    { c: 'use', arg: 'item', group: 'Cast', desc: 'Use an item, or an equipped slot: 13 = top trinket, 14 = bottom' },
    { c: 'castsequence', arg: 'seq', group: 'Cast', desc: 'Step through spells, one per press; reset= restarts it' },
    { c: 'castrandom', arg: 'list', group: 'Cast', desc: 'Cast one of the listed spells at random' },
    { c: 'stopcasting', arg: 'none', group: 'Cast', desc: 'Cancel the spell you are casting (e.g. before an interrupt)' },
    { c: 'cancelaura', arg: 'aura', group: 'Cast', desc: 'Remove a buff from yourself' },
    { c: 'cancelform', arg: 'none', group: 'Cast', desc: 'Leave your current form (not warrior stances)' },
    { c: 'startattack', arg: 'none', group: 'Combat', desc: 'Turn on auto-attack (@unit picks who)' },
    { c: 'stopattack', arg: 'none', group: 'Combat', desc: 'Turn off auto-attack' },
    { c: 'target', arg: 'text', group: 'Targeting', desc: 'Target a unit (@unit) or a name' },
    { c: 'targetenemy', arg: 'none', group: 'Targeting', desc: 'Tab-target the nearest enemy' },
    { c: 'targetfriend', arg: 'none', group: 'Targeting', desc: 'Target the nearest friendly player' },
    { c: 'targetlasttarget', arg: 'none', group: 'Targeting', desc: 'Go back to your previous target' },
    { c: 'cleartarget', arg: 'none', group: 'Targeting', desc: 'Clear your target' },
    { c: 'assist', arg: 'text', group: 'Targeting', desc: 'Target what your target (or @unit / a name) is targeting' },
    { c: 'focus', arg: 'text', group: 'Targeting', desc: 'Set your focus (to @unit, a name, or your target)' },
    { c: 'clearfocus', arg: 'none', group: 'Targeting', desc: 'Clear your focus' },
    { c: 'petattack', arg: 'none', group: 'Pet', desc: 'Send your pet at your target (or @unit)' },
    { c: 'petfollow', arg: 'none', group: 'Pet', desc: 'Pet follows you' },
    { c: 'petstay', arg: 'none', group: 'Pet', desc: 'Pet stays put' },
    { c: 'petpassive', arg: 'none', group: 'Pet', desc: 'Pet passive' },
    { c: 'petdefensive', arg: 'none', group: 'Pet', desc: 'Pet defensive' },
    { c: 'stopmacro', arg: 'none', group: 'Flow', desc: 'Stop here if the conditions match' },
    { c: 'dismount', arg: 'none', group: 'Flow', desc: 'Get off your mount' },
    { c: 'equip', arg: 'itemname', group: 'Flow', desc: 'Equip an item by name' },
  ];
  const COMMAND = Object.fromEntries(COMMANDS.map(c => [c.c, c]));

  // ── conditionals ──────────────────────────────────────────────────────
  // Structured bracket fields, in the order they're written out. Each has
  // the tokens its select offers ('' = not set).
  const MODS = ['shift', 'ctrl', 'alt'];
  const BRACKET_FIELDS = [
    { k: 'u', label: 'Target', opts: null /* units, see UNITS */ },
    { k: 'r', label: 'Help / harm', opts: ['help', 'harm', 'nohelp', 'noharm'] },
    { k: 'e', label: 'Exists', opts: ['exists', 'noexists'] },
    { k: 'd', label: 'Dead', opts: ['nodead', 'dead'] },
    { k: 'm', label: 'Modifier', opts: ['mod:shift', 'mod:ctrl', 'mod:alt', 'mod', 'nomod'] },
    { k: 'c', label: 'Combat', opts: ['combat', 'nocombat'] },
    { k: 's', label: 'Stance / form', opts: null /* per class, see STANCES */ },
    { k: 'st', label: 'Stealth', opts: ['stealth', 'nostealth'] },
    { k: 'mt', label: 'Mounted', opts: ['mounted', 'nomounted'] },
    { k: 'ch', label: 'Channeling', opts: ['channeling', 'nochanneling'] },
    { k: 'b', label: 'Mouse button', opts: ['button:1', 'button:2', 'button:3'] },
    { k: 'sp', label: 'Spec', opts: ['spec:1', 'spec:2'] },
    { k: 'pt', label: 'Pet', opts: ['pet', 'nopet'] },
    { k: 'gr', label: 'Group', opts: ['group', 'group:party', 'group:raid', 'nogroup'] },
    { k: 'io', label: 'Indoors', opts: ['indoors', 'outdoors'] },
    { k: 'sw', label: 'Swimming', opts: ['swimming', 'noswimming'] },
    { k: 'x', label: 'Other', opts: null /* free comma-separated tokens, linted */ },
  ];

  const UNITS = [
    { v: 'mouseover', label: '@mouseover — unit under the cursor (frames or world)' },
    { v: 'target', label: '@target' },
    { v: 'focus', label: '@focus' },
    { v: 'player', label: '@player — yourself' },
    { v: 'cursor', label: '@cursor — drop a ground spell at the cursor' },
    { v: 'pet', label: '@pet' },
    { v: 'targettarget', label: "@targettarget — your target's target" },
    { v: 'focustarget', label: "@focustarget — your focus's target" },
    { v: 'party1', label: '@party1' }, { v: 'party2', label: '@party2' },
    { v: 'party3', label: '@party3' }, { v: 'party4', label: '@party4' },
    { v: 'none', label: '@none — no target (click to aim)' },
  ];
  const UNIT_RE = /^(player|target|focus|mouseover|pet|none|cursor|party[1-4]|partypet[1-4]|raid([1-9]|[1-3]\d|40)|raidpet([1-9]|[1-3]\d|40))(target)*$/;

  // Stance-bar numbers per class. Hand-encoded; the order is the stance bar's,
  // which for druids and paladins depends on what you've learned.
  const STANCES = {
    warrior: { forms: [[1, 'Battle Stance'], [2, 'Defensive Stance'], [3, 'Berserker Stance']] },
    druid: {
      forms: [[1, 'Bear / Dire Bear Form'], [2, 'Aquatic Form'], [3, 'Cat Form'], [4, 'Travel Form'], [5, 'Moonkin Form']],
      note: 'Druid form numbers follow your stance bar: a druid without Aquatic Form has Cat Form as 2.',
      provisional: true,
    },
    rogue: { forms: [[1, 'Stealth']] },
    priest: { forms: [[1, 'Shadowform']] },
    shaman: { forms: [[1, 'Ghost Wolf']] },
    paladin: {
      forms: [1, 2, 3, 4, 5, 6, 7].map(n => [n, `Aura #${n} on your stance bar`]),
      note: 'Paladin auras sit on the stance bar in the order you learned them.',
      provisional: true,
    },
  };

  // Every conditional the linter accepts. neg: has a no- form. arg: 'none',
  // 'opt' (optional :value), 'req' (needs :value). prov: verify in-game.
  const KNOWN = {
    help: { neg: true, arg: 'none' }, harm: { neg: true, arg: 'none' },
    exists: { neg: true, arg: 'none' }, dead: { neg: true, arg: 'none' },
    combat: { neg: true, arg: 'none' }, stealth: { neg: true, arg: 'none' },
    mounted: { neg: true, arg: 'none' }, swimming: { neg: true, arg: 'none' },
    indoors: { neg: false, arg: 'none' }, outdoors: { neg: false, arg: 'none' },
    resting: { neg: true, arg: 'none' }, party: { neg: true, arg: 'none' }, raid: { neg: true, arg: 'none' },
    mod: { neg: true, arg: 'opt', vals: /^((shift|ctrl|alt)(\/(shift|ctrl|alt))*)$/ },
    modifier: { neg: true, arg: 'opt', vals: /^((shift|ctrl|alt)(\/(shift|ctrl|alt))*)$/ },
    channeling: { neg: true, arg: 'opt' },
    stance: { neg: true, arg: 'opt', vals: /^\d(\/\d)*$/ }, form: { neg: true, arg: 'opt', vals: /^\d(\/\d)*$/ },
    button: { neg: true, arg: 'req', vals: /^[1-5](\/[1-5])*$/ }, btn: { neg: true, arg: 'req', vals: /^[1-5](\/[1-5])*$/ },
    pet: { neg: true, arg: 'opt' },
    group: { neg: true, arg: 'opt', vals: /^(party|raid)$/ },
    equipped: { neg: true, arg: 'req' }, worn: { neg: true, arg: 'req' },
    actionbar: { neg: false, arg: 'req', vals: /^\d(\/\d)*$/ }, bar: { neg: false, arg: 'req', vals: /^\d(\/\d)*$/ },
    spec: { neg: false, arg: 'req', vals: /^[12]$/, prov: '[spec:1] / [spec:2] probably map to Forever\'s Primary / Secondary talent setups.' },
    talent: { neg: false, arg: 'req', vals: /^\d+\/\d+$/, prov: '[talent:row/column] comes from the retail talent grid; how it counts Forever\'s classic-shaped trees is unconfirmed.' },
    known: { neg: true, arg: 'req', prov: '[known:] comes from the modern client; unconfirmed on Forever.' },
  };
  // Retail-only / not meaningful in a level-60 classic world. Refused by the linter.
  const EXCLUDED = {
    covenant: 'Shadowlands covenants', pvptalent: 'retail PvP talents', petbattle: 'pet battles',
    vehicleui: 'vehicles', unithasvehicleui: 'vehicles', canexitvehicle: 'vehicles',
    flyable: 'there is no flying in a classic world', flying: 'there is no flying in a classic world',
    advflyable: 'dragonriding', bonusbar: 'use stance: instead', overridebar: 'retail override bars',
    possessbar: 'retail possess bar', extrabar: 'retail extra action button', shapeshift: 'retail vehicle-style bar',
    warmode: 'retail War Mode', spec3: 'no third spec',
  };
  const EXCLUDED_UNITS = { arena: 'Classic has no arenas', boss: 'no @boss units on a classic client' };

  function parseToken(tok) {
    tok = tok.trim();
    if (tok.startsWith('@') || tok.startsWith('target=')) {
      return { unit: tok.startsWith('@') ? tok.slice(1) : tok.slice(7) };
    }
    const m = tok.match(/^(no)?([a-z0-9]+)(?::(.+))?$/i);
    if (!m) return { bad: tok };
    let [, no, name, arg] = m;
    name = name.toLowerCase();
    // "nodead" parses as no + dead, but "none"/"nomod" etc. need care: prefer the full name if known.
    if (no && KNOWN['no' + name]) { name = 'no' + name; no = undefined; }
    return { neg: !!no, name, arg: arg ?? null, raw: tok };
  }

  // ── rendering ─────────────────────────────────────────────────────────
  function bracketTokens(br) {
    const out = [];
    if (!br) return out;
    if (br.u) out.push('@' + br.u);
    for (const f of BRACKET_FIELDS) {
      if (f.k === 'u' || f.k === 'x') continue;
      if (br[f.k]) out.push(br[f.k]);
    }
    if (br.x) out.push(...String(br.x).split(',').map(t => t.trim()).filter(Boolean));
    return out;
  }
  const bracketStr = br => `[${bracketTokens(br).join(',')}]`;
  const groupsStr = g => (g || []).map(bracketStr).join('');

  function argStr(a) {
    if (!a || !a.n) return '';
    const name = String(a.n).trim();
    return (a.t ? '!' : '') + name + (a.r ? `(Rank ${a.r})` : '');
  }

  function resetStr(rs) {
    if (!rs) return '';
    const parts = [];
    if (rs.s) parts.push(String(rs.s));
    if (rs.t) parts.push('target');
    if (rs.c) parts.push('combat');
    if (rs.m) parts.push(rs.m);
    return parts.length ? `reset=${parts.join('/')}` : '';
  }

  function branchStr(cmd, b) {
    const spec = COMMAND[cmd] || { arg: 'text' };
    let arg = '';
    if (spec.arg === 'seq') arg = [resetStr(b.rs), (b.q || []).map(argStr).filter(Boolean).join(', ')].filter(Boolean).join(' ');
    else if (spec.arg === 'list') arg = (b.q || []).map(argStr).filter(Boolean).join(', ');
    else if (spec.arg !== 'none') arg = argStr(b.a);
    const g = groupsStr(b.g);
    return [g, arg].filter(Boolean).join(' ');
  }

  function lineStr(line) {
    const body = (line.b && line.b.length ? line.b : [{}]).map(b => branchStr(line.c, b));
    // Trailing empty branches add nothing; an empty middle branch is kept as "" so ";;" shows up in lint.
    while (body.length > 1 && !body[body.length - 1]) body.pop();
    const text = body.join('; ');
    return `/${line.c}${text ? ' ' + text : ''}`;
  }

  function render(cfg) {
    const out = [];
    if (cfg.sh) out.push((cfg.sh === 2 ? '#show' : '#showtooltip') + (cfg.shn && cfg.shn.n ? ' ' + argStr({ ...cfg.shn, t: false }) : ''));
    for (const line of cfg.l || []) out.push(lineStr(line));
    return out.join('\n');
  }

  // Characters as the macro window counts them (each newline is one).
  const charCount = text => [...text].length;

  // ── lint ──────────────────────────────────────────────────────────────
  // ctx: { cls: slug, spells: [{name, ranks, target, form, pet}], className }
  function lint(cfg, ctx = {}) {
    const out = [];
    const add = (level, msg, line) => out.push({ level, msg, line });
    const byName = new Map((ctx.spells || []).map(s => [s.name.toLowerCase(), s]));
    const provisional = new Set();
    const text = render(cfg);
    const n = charCount(text);
    if (n > MAX_CHARS) add('error', `${n} characters: ${n - MAX_CHARS} over the ${MAX_CHARS}-character limit. The game won't save it.`);
    if (!(cfg.l || []).length) add('error', 'The macro has no command lines.');

    const checkArg = (a, where, line, { spellOnly = false } = {}) => {
      if (!a || !a.n || !String(a.n).trim()) { add('error', `${where}: pick a spell or item.`, line); return null; }
      const name = String(a.n).trim();
      if (/[\[\];\n]/.test(name)) { add('error', `${where}: “${name}” contains [ ] ; or a line break, which breaks the macro.`, line); return null; }
      if (/^\d+$/.test(name)) return null;  // equipment slot for /use
      const s = byName.get(name.toLowerCase());
      if (!s) {
        if (ctx.spells) add('info', `“${name}” isn't a ${ctx.className || 'class'} spell in the data. Fine for items, racials and other names; check the spelling.`, line);
        return null;
      }
      if (a.r && a.r > (s.ranks || 1)) add('error', `${name} has ${s.ranks || 1} rank${(s.ranks || 1) > 1 ? 's' : ''}; there's no Rank ${a.r}.`, line);
      if (spellOnly && s.pet && !a.t) add('info', `${name} is a pet ability: /cast sends it to your pet.`, line);
      return s;
    };

    (cfg.l || []).forEach((line, i) => {
      const ln = i + 1;
      const spec = COMMAND[line.c];
      if (!spec) { add('error', `Line ${ln}: /${line.c} isn't a command this builder knows.`, ln); return; }
      const branches = line.b && line.b.length ? line.b : [{}];
      const seen = new Set();
      let unconditional = false;
      branches.forEach((b, j) => {
        const where = branches.length > 1 ? `Line ${ln}, branch ${j + 1}` : `Line ${ln}`;
        if (unconditional) add('warn', `${where} never runs: an earlier branch has no conditions, so it always wins.`, ln);
        const groups = b.g || [];
        if (!groups.length || groups.some(br => !bracketTokens(br).length)) unconditional = true;
        const key = groupsStr(groups);
        if (groups.length && seen.has(key)) add('warn', `${where} repeats an earlier branch's conditions, so it never runs.`, ln);
        seen.add(key);

        const spells = [];
        if (spec.arg === 'spell' || spec.arg === 'aura') spells.push(checkArg(b.a, where, ln, { spellOnly: spec.arg === 'spell' }));
        if (spec.arg === 'item' && (!b.a || !b.a.n)) add('error', `${where}: pick an item or a slot (13 / 14).`, ln);
        if (spec.arg === 'itemname' && (!b.a || !b.a.n)) add('error', `${where}: type an item name.`, ln);
        if (spec.arg === 'seq' || spec.arg === 'list') {
          const q = (b.q || []).filter(a => a && a.n);
          if (q.length < 2) add('warn', `${where}: /${line.c} with fewer than two spells is just /cast.`, ln);
          q.forEach((a, k) => spells.push(checkArg(a, `${where}, spell ${k + 1}`, ln)));
          if (spec.arg === 'seq' && b.rs && b.rs.s && !(b.rs.s > 0 && b.rs.s <= 600)) add('error', `${where}: reset seconds must be 1–600.`, ln);
        }

        groups.forEach(br => {
          const toks = bracketTokens(br).map(parseToken);
          const names = new Map();
          let unit = null;
          for (const t of toks) {
            if (t.bad) { add('error', `${where}: “${t.bad}” isn't a valid conditional.`, ln); continue; }
            if (t.unit !== undefined) {
              if (unit) add('error', `${where}: two targets in one bracket (@${unit}, @${t.unit}).`, ln);
              unit = t.unit.toLowerCase();
              const base = unit.replace(/\d+$/, '').replace(/(target)+$/, '');
              if (!/^[a-z0-9-]+$/i.test(unit)) add('error', `${where}: “@${t.unit}” isn't a valid unit.`, ln);
              else if (EXCLUDED_UNITS[base]) add('error', `${where}: @${unit} — ${EXCLUDED_UNITS[base]}.`, ln);
              else if (!UNIT_RE.test(unit)) add('info', `${where}: @${t.unit} isn't a unit ID; it only works as a group member's name.`, ln);
              continue;
            }
            if (EXCLUDED[t.name]) { add('error', `${where}: [${t.raw}] isn't in Forever's macro set here (${EXCLUDED[t.name]}).`, ln); continue; }
            const k = KNOWN[t.name];
            if (!k) { add('error', `${where}: [${t.raw}] isn't a known conditional.`, ln); continue; }
            if (t.neg && !k.neg) add('error', `${where}: there's no “no${t.name}”.`, ln);
            if (k.arg === 'none' && t.arg != null) add('error', `${where}: ${t.name} doesn't take a value.`, ln);
            if (k.arg === 'req' && t.arg == null) add('error', `${where}: ${t.name} needs a value (${t.name}:…).`, ln);
            if (t.arg != null && k.vals && !k.vals.test(t.arg)) add('error', `${where}: “${t.raw}” has an invalid value.`, ln);
            if (k.prov) provisional.add(k.prov);
            const canon = t.name === 'form' ? 'stance' : t.name === 'modifier' ? 'mod' : t.name === 'btn' ? 'button' : t.name;
            if (names.has(canon) && names.get(canon) !== t.neg) add('error', `${where}: [${canon}] and [no${canon}] can't both be true.`, ln);
            names.set(canon, t.neg);
          }
          if (names.get('help') === false && names.get('harm') === false) add('error', `${where}: help and harm can't both be true.`, ln);
          if ((unit === 'player' || unit === 'cursor') && (names.has('help') || names.has('harm') || names.has('dead')))
            add('warn', `${where}: help/harm/dead on @${unit} is pointless.`, ln);
          if (names.has('stance') && ctx.cls && !STANCES[ctx.cls]) add('warn', `${where}: ${ctx.className || 'this class'} has no stances or forms.`, ln);
          if (names.has('stance') && STANCES[ctx.cls] && STANCES[ctx.cls].provisional) provisional.add(STANCES[ctx.cls].note);
          const sp = spells.find(Boolean);
          if (unit === 'cursor' && sp && sp.target !== 'ground') add('warn', `${where}: @cursor only aims ground-targeted spells; ${sp.name} isn't one.`, ln);
        });
      });
    });
    for (const p of provisional) add('info', `Verify in-game: ${p}`);
    return out;
  }

  // ── templates ─────────────────────────────────────────────────────────
  // Slot kinds: spell (with a suggestion filter), spells (a list), mod,
  // bool, number, choice, item. build(v) returns a config.
  const CASTERS = new Set(['mage', 'priest', 'warlock', 'shaman', 'druid', 'paladin']);
  const INTERRUPTS = ['Kick', 'Pummel', 'Shield Bash', 'Counterspell', 'Earth Shock', 'Silence', 'Feral Charge'];
  const SUGGEST = {
    help: s => s.target === 'help' || s.target === 'any',
    harm: s => s.target === 'harm' || s.target === 'any',
    ground: s => s.target === 'ground',
    interrupt: s => INTERRUPTS.includes(s.name),
    buff: s => s.target === 'self' && !s.pet,
    any: () => true,
  };
  const PREFER = {
    help: ['Flash Heal', 'Flash of Light', 'Lesser Healing Wave', 'Regrowth', 'Healing Touch', 'Renew', 'Power Word: Shield',
      'Remove Lesser Curse', 'Arcane Intellect', 'Mend Pet', 'Unending Breath', 'Intervene'],
    harm: ['Frostbolt', 'Shadow Bolt', 'Smite', 'Lightning Bolt', 'Wrath', 'Arcane Shot', 'Sinister Strike', 'Heroic Strike', 'Crusader Strike', 'Judgement'],
    ground: ['Blizzard', 'Flamestrike', 'Rain of Fire', 'Hurricane', 'Volley', 'Consecration', 'Flare'],
    interrupt: INTERRUPTS,
    buff: ['Ice Block', 'Divine Shield', 'Evasion', 'Barkskin', 'Shield Wall', 'Fade', 'Ice Barrier', 'Stealth', 'Prowl'],
    any: [],
  };
  const OPENERS = {
    rogue: ['Cheap Shot', 'Slice and Dice', 'Sinister Strike', 'Sinister Strike', 'Eviscerate'],
    warlock: ['Corruption', 'Bane of Agony', 'Immolate', 'Shadow Bolt'],
    priest: ['Shadow Word: Pain', 'Mind Blast', 'Mind Flay'],
    mage: ['Frostbolt', 'Frostbolt', 'Fire Blast'],
    warrior: ['Charge', 'Rend', 'Hamstring', 'Heroic Strike'],
    druid: ['Moonfire', 'Insect Swarm', 'Wrath'],
    hunter: ["Hunter's Mark", 'Serpent Sting', 'Arcane Shot'],
    shaman: ['Flame Shock', 'Lightning Bolt', 'Earth Shock'],
    paladin: ['Seal of Righteousness', 'Judgement'],
  };

  // Per-class defaults for the open-ended slots (first one the class has wins).
  const DUAL = {
    priest: ['Smite', 'Flash Heal'], mage: ['Frostbolt', 'Fireball'], warlock: ['Shadow Bolt', 'Searing Pain'],
    rogue: ['Sinister Strike', 'Backstab'], warrior: ['Heroic Strike', 'Cleave'], druid: ['Wrath', 'Starfire'],
    hunter: ['Arcane Shot', 'Multi-Shot'], shaman: ['Lightning Bolt', 'Chain Lightning'], paladin: ['Holy Light', 'Flash of Light'],
  };
  const BURST = {
    priest: ['Power Infusion', 'Inner Focus'], mage: ['Arcane Power', 'Presence of Mind', 'Combustion'],
    warlock: ['Shadow Bolt'], rogue: ['Adrenaline Rush', 'Blade Flurry', 'Cold Blood'], warrior: ['Death Wish', 'Recklessness'],
    druid: ["Nature's Swiftness", 'Starfire'], hunter: ['Rapid Fire'], shaman: ["Nature's Swiftness", 'Lightning Bolt'],
    paladin: ['Divine Favor', 'Holy Light'],
  };

  const A = (n, r = 0) => (n ? { n, r } : null);

  const TEMPLATES = [
    {
      id: 'mouseover-help', name: 'Mouseover heal', blurb: 'Heals or buffs whoever is under your mouse (party frames or the world), falls back to your friendly target, then yourself.',
      slots: [
        { k: 'spell', kind: 'spell', label: 'Heal / buff', suggest: 'help', strict: true },
        { k: 'self', kind: 'bool', label: 'Fall back to yourself', def: true },
        { k: 'mod', kind: 'mod', label: 'Hold to self-cast', def: '', none: 'No self-cast key' },
      ],
      build: v => {
        const g = [];
        if (v.mod) g.push({ u: 'player', m: 'mod:' + v.mod });
        g.push({ u: 'mouseover', r: 'help', d: 'nodead' }, { r: 'help', d: 'nodead' });
        if (v.self) g.push({ u: 'player' });
        return { sh: 1, l: [{ c: 'cast', b: [{ g, a: v.spell }] }] };
      },
    },
    {
      id: 'mouseover-harm', name: 'Mouseover attack', blurb: 'Casts on the enemy under your mouse without changing target; otherwise casts normally at your target.',
      slots: [
        { k: 'spell', kind: 'spell', label: 'Spell', suggest: 'harm' },
        { k: 'fallback', kind: 'bool', label: 'Fall back to your target', def: true },
      ],
      build: v => ({ sh: 1, l: [{ c: 'cast', b: [{ g: [{ u: 'mouseover', r: 'harm', d: 'nodead' }, ...(v.fallback ? [{}] : [])], a: v.spell }] }] }),
    },
    {
      id: 'focus', name: 'Focus cast / interrupt', blurb: 'Casts on your focus (interrupt, crowd control) while you keep hitting your target. Shift-click sets the focus.',
      slots: [
        { k: 'spell', kind: 'spell', label: 'Spell', suggest: 'interrupt', fallbackSuggest: 'harm' },
        { k: 'setfocus', kind: 'bool', label: 'Shift-click sets focus', def: true },
        { k: 'stop', kind: 'bool', label: '/stopcasting first', def: ctx => CASTERS.has(ctx.cls) },
        { k: 'fallback', kind: 'bool', label: 'No focus: cast at target', def: true },
      ],
      build: v => {
        const l = [];
        if (v.setfocus) l.push({ c: 'focus', b: [{ g: [{ m: 'mod:shift' }] }] }, { c: 'stopmacro', b: [{ g: [{ m: 'mod:shift' }] }] });
        if (v.stop) l.push({ c: 'stopcasting', b: [] });
        l.push({ c: 'cast', b: [{ g: [{ u: 'focus', r: 'harm', d: 'nodead' }, ...(v.fallback ? [{}] : [])], a: v.spell }] });
        return { sh: 1, shn: v.setfocus ? v.spell : null, l };
      },
    },
    {
      id: 'mod-dual', name: 'Modifier: two or three spells', blurb: 'One button, different spell while a modifier is held: shift = one spell, (ctrl = another,) otherwise the main spell.',
      slots: [
        { k: 'main', kind: 'spell', label: 'No modifier', suggest: 'any', prefer: DUAL, pick: 0 },
        { k: 'mod1', kind: 'mod', label: 'Modifier 1', def: 'shift' },
        { k: 'alt1', kind: 'spell', label: 'Spell with modifier 1', suggest: 'any', prefer: DUAL, pick: 1 },
        { k: 'mod2', kind: 'mod', label: 'Modifier 2', def: '', none: 'None' },
        { k: 'alt2', kind: 'spell', label: 'Spell with modifier 2', suggest: 'any', pick: 2, optional: true, when: v => !!v.mod2 },
      ],
      build: v => {
        const b = [{ g: [{ m: 'mod:' + (v.mod1 || 'shift') }], a: v.alt1 }];
        if (v.mod2 && v.alt2) b.push({ g: [{ m: 'mod:' + v.mod2 }], a: v.alt2 });
        b.push({ g: [], a: v.main });
        return { sh: 1, l: [{ c: 'cast', b }] };
      },
    },
    {
      id: 'trinket', name: 'Trinkets + ability', blurb: 'Pops your trinket(s) and casts an ability in one press. The tooltip shows the ability.',
      slots: [
        { k: 'spell', kind: 'spell', label: 'Ability', suggest: 'any', prefer: BURST },
        { k: 't13', kind: 'bool', label: '/use 13 (top trinket)', def: true },
        { k: 't14', kind: 'bool', label: '/use 14 (bottom trinket)', def: false },
        { k: 'item', kind: 'item', label: 'Also use an item (optional)', optional: true },
      ],
      build: v => {
        const l = [];
        if (v.t13) l.push({ c: 'use', b: [{ g: [], a: { n: '13' } }] });
        if (v.t14) l.push({ c: 'use', b: [{ g: [], a: { n: '14' } }] });
        if (v.item && v.item.n) l.push({ c: 'use', b: [{ g: [], a: v.item }] });
        l.push({ c: 'cast', b: [{ g: [], a: v.spell }] });
        return { sh: 1, shn: v.spell, l };
      },
    },
    {
      id: 'castsequence', name: 'Cast sequence (opener)', blurb: 'Each press casts the next spell in the list. A press that fails (cooldown, out of range) doesn\'t advance it.',
      slots: [
        { k: 'spells', kind: 'spells', label: 'Spells, in order', min: 2, max: 8 },
        { k: 'secs', kind: 'number', label: 'Reset after N seconds idle (0 = off)', def: 0, min: 0, max: 600 },
        { k: 'rtarget', kind: 'bool', label: 'Reset on target change', def: true },
        { k: 'rcombat', kind: 'bool', label: 'Reset when leaving combat', def: true },
        { k: 'rmod', kind: 'mod', label: 'Reset with modifier', def: '', none: 'None' },
        { k: 'attack', kind: 'bool', label: '/startattack first', def: ctx => !CASTERS.has(ctx.cls) },
      ],
      build: v => {
        const l = [];
        if (v.attack) l.push({ c: 'startattack', b: [] });
        l.push({ c: 'castsequence', b: [{ g: [], rs: { s: Number(v.secs) || 0, t: !!v.rtarget, c: !!v.rcombat, m: v.rmod || '' }, q: v.spells || [] }] });
        return { sh: 1, l };
      },
    },
    {
      id: 'startattack', name: 'Start attack + ability', blurb: 'Grabs the nearest enemy if you have no live hostile target, turns on auto-attack, and casts.',
      slots: [
        { k: 'spell', kind: 'spell', label: 'Ability', suggest: 'harm' },
        { k: 'autotarget', kind: 'bool', label: 'Target nearest enemy if needed', def: true },
      ],
      build: v => {
        const l = [];
        if (v.autotarget) l.push({ c: 'targetenemy', b: [{ g: [{ r: 'noharm' }, { d: 'dead' }] }] });
        l.push({ c: 'startattack', b: [] }, { c: 'cast', b: [{ g: [], a: v.spell }] });
        return { sh: 1, l };
      },
    },
    {
      id: 'cancelaura', name: 'Cancel-aura toggle', blurb: 'Cast a buff, or cancel it: press again (buffs with a cooldown, like Ice Block) or hold a modifier (any buff).',
      slots: [
        { k: 'spell', kind: 'spell', label: 'Buff', suggest: 'buff' },
        { k: 'mode', kind: 'choice', label: 'Cancel it by', def: 'mod', opts: [['mod', 'Holding a modifier'], ['press', 'Pressing again (cooldown buffs only)']] },
        { k: 'mod', kind: 'mod', label: 'Modifier', def: 'alt', when: v => v.mode === 'mod' },
      ],
      build: v => {
        const name = v.spell ? { n: v.spell.n } : null;  // /cancelaura takes the buff name, no rank
        if (v.mode === 'press') return { sh: 1, shn: name, l: [{ c: 'cancelaura', b: [{ g: [], a: name }] }, { c: 'cast', b: [{ g: [], a: v.spell }] }] };
        const g = [{ m: 'mod:' + (v.mod || 'alt') }];
        return { sh: 1, shn: name, l: [{ c: 'cancelaura', b: [{ g, a: name }] }, { c: 'stopmacro', b: [{ g }] }, { c: 'cast', b: [{ g: [], a: v.spell }] }] };
      },
    },
    {
      id: 'cursor', name: 'Cast at cursor (ground AoE)', blurb: 'Drops a ground-targeted spell where your mouse is, no targeting circle. Optionally a modifier drops it on yourself.',
      slots: [
        { k: 'spell', kind: 'spell', label: 'Ground spell', suggest: 'ground', strict: true },
        { k: 'mod', kind: 'mod', label: 'Hold to drop it on yourself', def: '', none: 'No' },
      ],
      build: v => ({ sh: 1, l: [{ c: 'cast', b: [{ g: [...(v.mod ? [{ m: 'mod:' + v.mod, u: 'player' }] : []), { u: 'cursor' }], a: v.spell }] }] }),
    },
  ];
  const TEMPLATE = Object.fromEntries(TEMPLATES.map(t => [t.id, t]));

  // Spells for a slot: [suggested, rest], each sorted by name.
  function suggestions(slot, spells) {
    const own = (spells || []).filter(s => !s.pet);
    let f = SUGGEST[slot.suggest || 'any'];
    let hit = own.filter(f);
    if (!hit.length && slot.fallbackSuggest) { f = SUGGEST[slot.fallbackSuggest]; hit = own.filter(f); }
    if (slot.suggest === 'any' || !slot.suggest) hit = [];
    const byName = (a, b) => a.name.localeCompare(b.name);
    return { suggested: hit.slice().sort(byName), rest: own.filter(s => !hit.includes(s)).sort(byName), pet: (spells || []).filter(s => s.pet).sort(byName) };
  }

  function defaultSpell(slot, ctx, avoid = []) {
    const spells = ctx.spells;
    const { suggested, rest } = suggestions(slot, spells);
    if (slot.strict && !suggested.length) return null;  // e.g. no ground spells: leave it for the user
    const pool = suggested.length ? suggested : rest;
    const own = slot.prefer && slot.prefer[ctx.cls];
    if (own) {
      const names = slot.pick != null ? own.slice(slot.pick) : own;
      const hit = names.map(n => pool.find(s => s.name === n)).find(s => s && !avoid.includes(s.name));
      if (hit) return A(hit.name);
    }
    const prefer = PREFER[slot.suggest] || PREFER.harm;
    const hit = prefer.map(n => pool.find(s => s.name === n)).find(s => s && !avoid.includes(s.name))
      || pool.filter(s => !s.talent).sort((a, b) => b.level - a.level || b.ranks - a.ranks).find(s => !avoid.includes(s.name))
      || pool[0];
    return hit ? A(hit.name) : null;
  }

  // Default slot values for a class. ctx = { cls, spells }
  function templateDefaults(tpl, ctx) {
    const v = {};
    const picked = [];
    for (const s of tpl.slots) {
      const def = typeof s.def === 'function' ? s.def(ctx) : s.def;
      if (s.kind === 'spell') {
        if (s.optional) { v[s.k] = null; continue; }
        const a = defaultSpell(s, ctx, picked);
        v[s.k] = a; if (a) picked.push(a.n);
      } else if (s.kind === 'spells') {
        const names = (OPENERS[ctx.cls] || []).filter(n => (ctx.spells || []).some(sp => sp.name === n));
        v[s.k] = (names.length >= 2 ? names : (ctx.spells || []).filter(sp => sp.target === 'harm' && !sp.pet).slice(-3).map(sp => sp.name)).map(n => A(n));
      } else if (s.kind === 'item') v[s.k] = null;
      else v[s.k] = def ?? '';
    }
    return v;
  }

  // ── sharing ───────────────────────────────────────────────────────────
  // State in the URL: { t: template id, v: slot values } or { f: config }.
  function encode(state) {
    const bytes = new TextEncoder().encode(JSON.stringify(state));
    let bin = '';
    bytes.forEach(b => { bin += String.fromCharCode(b); });
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function decode(s) {
    try {
      const bin = atob(s.replace(/-/g, '+').replace(/_/g, '/'));
      const st = JSON.parse(new TextDecoder().decode(Uint8Array.from(bin, c => c.charCodeAt(0))));
      if (st && (TEMPLATE[st.t] || (st.f && Array.isArray(st.f.l)))) return st;
    } catch (e) { /* fall through */ }
    return null;
  }

  root.Macro = {
    MAX_CHARS, COMMANDS, COMMAND, BRACKET_FIELDS, UNITS, STANCES, KNOWN, EXCLUDED, MODS, TEMPLATES, TEMPLATE,
    render, lineStr, bracketTokens, argStr, charCount, lint, suggestions, templateDefaults, encode, decode, parseToken,
  };
})(typeof window !== 'undefined' ? window : globalThis);
