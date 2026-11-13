<script>
  // Judgement tags review (docs/synergy-v2.md, Stage 1b). Each champion-role is rated 0-3 on eight traits that
  // match stats can't measure. The first pass was drafted by Claude (unreviewed: half weight in every model);
  // edits are written back to data/champion_tags.yaml. Measured traits are shown for context, read-only.
  import Champ from './Champ.svelte';
import PageHeader from './ui/PageHeader.svelte';
import Toolbar from './ui/Toolbar.svelte';
import ToolGroup from './ui/ToolGroup.svelte';
import Segmented from './ui/Segmented.svelte';
import Field from './ui/Field.svelte';
import Button from './ui/Button.svelte';
import Table from './ui/Table.svelte';
import Tag from './ui/Tag.svelte';
import Menu from './ui/Menu.svelte';

  const LABELS = { engage: 'Engage', disengage: 'Disengage', poke: 'Poke', dive: 'Dive', pick: 'Pick',
                   splitpush: 'Split', enchanter: 'Enchanter', hypercarry: 'Hypercarry', setup: 'Setup', followup: 'Follow-up',
                   needs_team: 'Needs team' };
  // kit interactions for matchups (docs/matchup-archetypes.md, Stage 2)
  const KIT_LABELS = {
    auto_reliant: ['Autos', 'Damage mainly from auto attacks'],
    auto_denial: ['Anti-auto', 'Blocks, dodges or slows auto attacks'],
    max_hp_damage: ['Max HP', '% max-health or true damage that shreds tanks'],
    hard_gap_close: ['Gap close', 'Reliable gap close through minions or kites'],
    kite: ['Kite', 'Can fight at range and move while doing it'],
    escape: ['Escape', 'Reliable disengage or escape'],
    extend_fight: ['Long fight', 'Forces long fights: pulls, sticky damage'],
    anti_dive: ['Anti-dive', 'Kit built to punish divers or assassins'],
    sustain: ['Sustain', 'Out-heals poke'],
    ability_range: ['Range', 'Long ability range and wave clear'],
  };
  const MEASURED = [['cc', 'CC'], ['frontline', 'Front'], ['peel', 'Peel'], ['power_curve', 'Scaling'],
                    ['lane_pressure', 'Lane']];
  // column headings short enough for eleven rating columns to fit; the full word is the heading's title
  const SHORT = { disengage: 'Diseng', enchanter: 'Ench', hypercarry: 'Hyper', followup: 'Follow', needs_team: 'Needs' };
  let data = $state(null);
  let filter = $state('review');
  let query = $state('');
  let roleFilter = $state('all');
  let edits = $state({}); // "champ:role" -> values
  let archEdits = $state({}); // "champ:role" -> {archetype, archetype2}
  let kitEdits = $state({}); // "champ:role" -> kit tag -> 0..3
  let view = $state('fight'); // the group under review: fight (synergy tags), kit, or arch (archetypes)
  let error = $state({});
  let adding = $state({}); // champ id -> role being added
let loadError = $state('');

async function load() {
 try {
  const res = await fetch('/api/tags');
  if (!res.ok) throw new Error(`request failed (${res.status})`);
  data = await res.json();
  loadError = '';
 } catch (e) { loadError = `Couldn't load champion tags: ${e.message}`; }
}
load();

  // one row per champion-role entry; untagged champions get one empty row
  const rows = $derived.by(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    const out = [];
    for (const c of data.champions) {
      if (q && !c.name.toLowerCase().includes(q)) continue;
      // tagged roles, plus archetype-only ones (written or edited: no 0-3 ratings yet)
      const roles = [...new Set([...Object.keys(c.entries), ...Object.keys(c.kit),
        ...Object.keys(c.archetypes).filter((r) => !c.archetypes[r].untagged)])];
      if (adding[c.champ_id] && !roles.includes(adding[c.champ_id])) roles.push(adding[c.champ_id]);
      if (!roles.length) {
        if (filter === 'untagged' || filter === 'all') out.push({ c, role: null, entry: null });
        continue;
      }
      for (const role of data.roles.filter((r) => roles.includes(r))) {
        if (roleFilter !== 'all' && role !== roleFilter) continue;
        const entry = c.entries[role] ?? null;
        if (filter === 'review' && !(inGroup(c, role, entry) && !reviewedIn(c, role, entry))) continue;
        if (filter === 'reviewed' && !(inGroup(c, role, entry) && reviewedIn(c, role, entry))) continue;
        if (filter === 'untagged') continue;
        out.push({ c, role, entry });
      }
    }
    return out;
  });

  // 250 rows of editors are slow to draw: the first 60 show, the rest on request (any filter or search starts over)
  const PAGE = 60;
  let showAll = $state(false);
  $effect(() => { filter; view; roleFilter; query; showAll = false; });
  const shownRows = $derived(showAll ? rows : rows.slice(0, PAGE));
  const key = (c, role) => `${c.champ_id}:${role}`;
  // does this line have labels in the group under review, and are they reviewed?
  function inGroup(c, role, entry) {
    if (view === 'kit') return !!c.kit[role];
    if (view === 'arch') return !!c.archetypes[role] && !c.archetypes[role].untagged;
    return !!entry;
  }
  function reviewedIn(c, role, entry) {
    if (view === 'kit') return !!c.kit_reviewed[role];
    if (view === 'arch') return !!c.archetypes[role]?.reviewed;
    return !!entry?.reviewed;
  }
  function values(c, role, entry) {
    return edits[key(c, role)] ?? { ...(entry?.values ?? Object.fromEntries(data.tags.map((t) => [t, 0]))) };
  }
  function setValue(c, role, entry, tag, v) {
    edits[key(c, role)] = { ...values(c, role, entry), [tag]: v };
  }
  const changed = (c, role, entry) => {
    const e = edits[key(c, role)];
    return (e && (!entry || data.tags.some((t) => e[t] !== entry.values[t]))) || archEdits[key(c, role)]
      || kitEdits[key(c, role)];
  };
  const kitValues = (c, role) => kitEdits[key(c, role)] ?? c.kit[role] ?? {};
  function setKit(c, role, tag, v) {
    kitEdits[key(c, role)] = { ...kitValues(c, role), [tag]: v };
  }
  // archetype and optional secondary, per role (docs/matchup-archetypes.md)
  function arch(c, role) {
    const a = c.archetypes[role];
    const written = a && !a.untagged;
    return archEdits[key(c, role)] ?? { archetype: written ? a.archetype : '', archetype2: written ? a.archetype2 ?? '' : '' };
  }
  function setArch(c, role, field, v) {
    const next = { ...arch(c, role), [field]: v };
    if (!next.archetype || next.archetype2 === next.archetype) next.archetype2 = '';
    archEdits[key(c, role)] = next;
  }
  const archLabel = (a) => a.replaceAll('_', ' ');

  function replace(res) {
    const i = data.champions.findIndex((x) => x.champ_id === res.champ_id);
    data.champions[i] = res;
    const entries = data.champions.flatMap((x) => Object.values(x.entries));
    data.counts = { champions: data.champions.length, entries: entries.length,
                    reviewed: entries.filter((e) => e.reviewed).length,
                    untagged: data.champions.filter((x) => x.untagged).length };
  }
  async function send(c, role, method, body) {
    const r = await fetch(`/api/tags/${c.champ_id}/${role}`, {
      method, headers: { 'Content-Type': 'application/json' }, body: body && JSON.stringify(body),
    });
    const res = await r.json();
    if (!r.ok) { error[key(c, role)] = res.detail; return; }
    delete error[key(c, role)];
    delete edits[key(c, role)];
    delete archEdits[key(c, role)];
    delete kitEdits[key(c, role)];
    delete adding[c.champ_id];
    replace(res);
  }
  // Saving marks the groups edited as reviewed; "Looks right" marks the group under review. Others are left alone.
  // A kit is sent only when edited: it applies to all of the champion's lines.
  function save(c, role, entry, looksRight = false) {
    const k = key(c, role);
    const fightEdited = edits[k] && (!entry || data.tags.some((t) => edits[k][t] !== entry.values[t]));
    send(c, role, 'PUT', {
      values: values(c, role, entry), ...arch(c, role),
      kit: kitEdits[k],
      reviewed: !!entry?.reviewed || !!fightEdited || (looksRight && view === 'fight'),
      reviewed_kit: kitEdits[k] || (looksRight && view === 'kit') ? true : undefined,
      reviewed_arch: archEdits[k] || (looksRight && view === 'arch') ? true : undefined,
    });
  }
  const counts = $derived.by(() => {
    if (!data) return { reviewed: 0, total: 0 };
    let reviewed = 0, total = 0;
    for (const c of data.champions) {
      const lines = view === 'kit' ? Object.keys(c.kit)
        : view === 'arch' ? Object.keys(c.archetypes).filter((r) => !c.archetypes[r].untagged)
        : Object.keys(c.entries);
      for (const r of lines) { total++; if (reviewedIn(c, r, c.entries[r])) reviewed++; }
    }
    return { reviewed, total };
  });
  const remove = (c, role) => send(c, role, 'DELETE');
  const fmt = (v) => (v == null ? '—' : Math.round(v * 100));
</script>

<main class="console">
<PageHeader title="Champion tags" description="Review fight style, matchup-kit interactions and lane archetypes for each champion role.">
 {#snippet actions()}<Button variant="ghost" href="#/settings">Settings</Button>{/snippet}
</PageHeader>
  {#if data}
<Toolbar label="Tag review filters">
 <ToolGroup label="Review">
  <Tag onclick={() => (filter = 'review')} pressed={filter === 'review'}>To review</Tag>
  <Tag onclick={() => (filter = 'reviewed')} pressed={filter === 'reviewed'}>Reviewed</Tag>
  <Tag onclick={() => (filter = 'untagged')} pressed={filter === 'untagged'} warn>Untagged</Tag>
  <Tag onclick={() => (filter = 'all')} pressed={filter === 'all'}>All</Tag>
 </ToolGroup>
 <ToolGroup label="View"><Segmented label="Tag group" options={[["fight", "Fight tags"], ["kit", "Kit tags"], ["arch", "Archetypes"]]} bind:value={view} /></ToolGroup>
 <ToolGroup label="Role"><Field label="Role" hideLabel options={[['all', 'All roles'], ...data.roles.map((r) => [r, data.role_labels[r]])]} bind:value={roleFilter} width="132px" /></ToolGroup>
 <ToolGroup end><Field label="Search champions" hideLabel placeholder="Search champions" bind:value={query} width="190px" /></ToolGroup>
</Toolbar>
<div class="summary">
 <div class="progress" title="{counts.reviewed} of {counts.total} reviewed" aria-label="{counts.reviewed} of {counts.total} reviewed"><span style="width:{counts.total ? (100 * counts.reviewed) / counts.total : 0}%"></span></div>
 <span class="mono">{counts.reviewed} / {counts.total}</span><span>reviewed</span><Tag warn={data.counts.untagged > 0}>{data.counts.untagged} untagged</Tag>
</div>
<p class="help">
      0 = not at all, 3 = defining. The tags are the one source of fight style: Style shows the identity they give
      (Engage, Poke, Pick, Protect, Split), used by the badges, comp panels, comp coherence and the Team page.
      Unreviewed tags count at half weight in the synergy and team models (labels read them as written); "Looks right"
      marks them reviewed. Untagged champions fall back to their Data Dragon classes. Timing (Early / Late) and the
      measured traits (percentile in the role, 0–100) come from crawled matches and aren't edited here.
      Archetype is per role, for matchups; a blank one uses the suggestion from the champion's class, shown in the
      list. "guide" marks one named in a matchup guide. Kit tags feed the matchup model; hover a column for its meaning.
      A kit rating belongs to the champion: changing it in one role changes all its roles. Each of Fight tags, Kit tags
      and Archetypes has its own review state; "Looks right" marks only the one you're viewing.
    </p>
<Table dense sticky={false} scrollX={true}>
      <thead><tr><th><span class="sr">Icon</span></th><th>Champion</th><th>Role</th><th>Archetype</th>
        {#if view === 'kit'}
          {#each data.kit_tags as t}<th class="num" title={KIT_LABELS[t][1]}>{KIT_LABELS[t][0]}</th>{/each}
        {:else if view === 'fight'}
          {#each data.tags as t}<th class="num" title={LABELS[t]}>{SHORT[t] ?? LABELS[t]}</th>{/each}
        {/if}
        {#if view === 'fight'}<th>Style</th><th class="xwide" title="Percentile in the role, from crawled matches">Measured</th>{/if}<th><span class="sr">Actions</span></th></tr></thead>
      <tbody>
        {#each shownRows as { c, role, entry } (key(c, role))}
          {@const v = role ? values(c, role, entry) : null}
          {@const m = role ? c.measured[role] : null}
          {@const hasArch = role && c.archetypes[role] && !c.archetypes[role].untagged}
          {@const here = role && inGroup(c, role, entry)}
          {@const isReviewed = !here || reviewedIn(c, role, entry)}
          <tr class:reviewed={here && isReviewed}>
            <td class="icon"><Champ champ={c.champ} size={24} /></td>
            <td><span class="who"><strong>{c.name}</strong>{#if c.untagged}<Tag warn>untagged</Tag>{/if}{#if !isReviewed}<Tag title="Drafted, not reviewed yet: counts at half weight">draft</Tag>{/if}</span></td>
            <td>{#if role}{data.role_labels[role]}{:else}
              <select onchange={(ev) => (adding[c.champ_id] = ev.target.value)}>
                <option value="">Add role…</option>
                {#each data.roles as r}<option value={r}>{data.role_labels[r]}</option>{/each}
              </select>{/if}</td>
            <td class="arch">{#if role}
              {@const a = arch(c, role)}
              {@const fb = c.archetypes[role]?.untagged ? c.archetypes[role].archetype : null}
              {#if view === 'arch'}
              <select aria-label="Archetype" value={a.archetype} onchange={(ev) => setArch(c, role, 'archetype', ev.target.value)}>
                <option value="">{fb ? `(${archLabel(fb)})` : '—'}</option>
                {#each data.archetypes[role] as w}<option value={w}>{archLabel(w)}</option>{/each}
              </select>
              <select aria-label="Secondary archetype" value={a.archetype2} disabled={!a.archetype}
                      onchange={(ev) => setArch(c, role, 'archetype2', ev.target.value)}>
                <option value="">—</option>
                {#each data.archetypes[role].filter((w) => w !== a.archetype) as w}<option value={w}>{archLabel(w)}</option>{/each}
              </select>
              {:else}
                <span class:quiet={!a.archetype}>{a.archetype ? archLabel(a.archetype) : fb ? `(${archLabel(fb)})` : '—'}{a.archetype2 ? ` / ${archLabel(a.archetype2)}` : ''}</span>
              {/if}
{#if c.archetypes[role]?.source === 'video'}<Tag>guide</Tag>{/if}
            {/if}</td>
            {#if view === 'kit'}
              {#each data.kit_tags as t}
                {@const kv = role ? kitValues(c, role)[t] ?? 0 : 0}
                <td class="num">{#if role}
                  <select class="lvl l{kv}" aria-label={KIT_LABELS[t][0]} value={kv} onchange={(ev) => setKit(c, role, t, +ev.target.value)}>
                    {#each [0, 1, 2, 3] as n}<option value={n}>{n}</option>{/each}
                  </select>{/if}</td>
              {/each}
            {:else if view === 'fight'}
              {#each data.tags as t}
                <td class="num">{#if role}
                  <select class="lvl l{v[t]}" value={v[t]} onchange={(ev) => setValue(c, role, entry, t, +ev.target.value)}>
                    {#each [0, 1, 2, 3] as n}<option value={n}>{n}</option>{/each}
                  </select>{/if}</td>
              {/each}
            {/if}
            {#if view === 'fight'}
            <td class="stylecol small">
              {#if role && c.identity[role]}
                {@const id = c.identity[role]}
                {@const t = c.timing[role]}
                <span title={Object.entries(id.shares).map(([k, v]) => `${data.styles[k]} ${Math.round(v * 100)}%`).join(' · ') + (m ? `\nMeasured (percentile in role): ${MEASURED.map(([k, l]) => `${l} ${fmt(m[k])}`).join(' · ')}` : '')}>
                  {id.badge.styles.map((k) => data.styles[k]).join(' / ') || 'Spread'}</span>
{#if t?.label === 'early' || t?.label === 'late'}<Tag>{data.timings[t.label]}</Tag>{/if}
{#if id.source === 'classes'}<span class="quiet">(classes)</span>{/if}
              {/if}
            </td>
            <td class="measured small xwide">
              {#if m}
                {#each MEASURED as [k, l]}<span title="{l}: percentile in role">{l} {fmt(m[k])}</span>{/each}
{:else if role}<span class="quiet">no data</span>{/if}
            </td>
            {/if}
            <td class="actions">
              {#if role}
                {#if changed(c, role, entry)}
<Button variant="primary" onclick={() => save(c, role, entry)}>Save</Button>
                {:else if !isReviewed}
<Button onclick={() => save(c, role, entry, true)}>Looks right</Button>
                {/if}
{#if entry || hasArch}<Menu label="More for {c.name} {data.role_labels[role]}" items={[{ label: 'Remove this role…', danger: true, onclick: () => remove(c, role) }]} />{/if}
              {/if}
              {#if error[key(c, role)]}<div class="err">{error[key(c, role)]}</div>{/if}
            </td>
          </tr>
        {:else}
<tr><td colspan={view === 'kit' ? data.kit_tags.length + 5 : view === 'arch' ? 5 : data.tags.length + 7} class="quiet">Nothing matches these filters.</td></tr>
        {/each}
      </tbody>
</Table>
{#if rows.length > shownRows.length}
  <p class="more">Showing the first {shownRows.length} of {rows.length}. <Button variant="ghost" onclick={() => (showAll = true)}>Show all {rows.length}</Button></p>
{/if}
{:else if loadError}
<p class="message error" role="alert">⚠ {loadError} <Button variant="ghost" onclick={load}>Try again</Button></p>
{:else}
<p class="message" aria-busy="true">Loading champion tags…</p>
  {/if}
</main>

<style>
.summary { display: flex; align-items: center; gap: 8px; min-height: 28px; margin: 0 0 12px; color: var(--text-3); font-size: 12px; }
.summary .mono { color: var(--text-1); }
.progress { width: 128px; height: 4px; overflow: hidden; background: var(--surface-2); border-radius: 2px; }
.progress span { display: block; height: 100%; background: var(--text-2); }
.help { max-width: 110ch; margin: 0 0 16px; color: var(--text-3); font-size: 12px; line-height: 1.55; }
select.lvl { appearance: none; -webkit-appearance: none; width: 28px; height: 24px; padding: 0; text-align: center; text-align-last: center; cursor: pointer; background: var(--surface-2); color: var(--text-1); border: 1px solid var(--hairline); border-radius: var(--radius-sm); font: 12px var(--font-mono); }
select.lvl:hover { border-color: var(--hairline-strong); }
select.lvl:focus-visible, .arch select:focus-visible { outline: none; border-color: var(--signal); }
select.l0 { color: var(--text-3); }
.measured { white-space: nowrap; color: var(--text-3); }
  .stylecol { white-space: nowrap; }
  .arch { white-space: nowrap; }
.arch select { width: 148px; height: 24px; margin-right: 4px; padding: 0 4px; background: var(--surface-2); color: var(--text-1); border: 1px solid var(--hairline); border-radius: var(--radius-sm); font-size: 12px; }
  .arch select:disabled { opacity: 0.45; }
  .measured span { margin-right: 8px; }
  .actions { white-space: nowrap; }
.actions :global(.btn + .btn) { margin-left: 4px; }
.err, .error { color: var(--error); font-size: 12px; }
.quiet { color: var(--text-3); }
.message { margin: 16px 0; color: var(--text-3); }
  .small { font-size: 12px; }
  .who { display: inline-flex; align-items: baseline; gap: 6px; white-space: nowrap; }
  .who strong { font-weight: 600; }
  .icon { width: 32px; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  .more { margin: 12px 0 0; font-size: 13px; color: var(--text-3); }
  /* eleven rating columns: tight cells, so the table fits without scrolling sideways */
  main :global(td.num), main :global(th.num) { padding-left: 4px; padding-right: 4px; }
  main :global(th.num) { font-size: 10px; letter-spacing: 0.03em; }
  @media (max-width: 1640px) { main :global(.xwide) { display: none; } }
</style>
