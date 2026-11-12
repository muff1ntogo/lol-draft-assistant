<script>
  // #/kit: every shared console component (web/src/ui/) in every state, for checking them while the pages are
  // rebuilt on them (docs/ui-redesign.md, step 1). Not in the rail; removed in step 9. The table reads real rows
  // (the champion tags) so it is checked with real names and icons; the rest are fixed samples.
  import Champ from './Champ.svelte';
  import PageHeader from './ui/PageHeader.svelte';
  import Toolbar from './ui/Toolbar.svelte';
  import ToolGroup from './ui/ToolGroup.svelte';
  import Segmented from './ui/Segmented.svelte';
  import Button from './ui/Button.svelte';
  import Table from './ui/Table.svelte';
  import Panel from './ui/Panel.svelte';
  import Stat from './ui/Stat.svelte';
  import Notice from './ui/Notice.svelte';
  import Field from './ui/Field.svelte';
  import Tag from './ui/Tag.svelte';

  // ---- the table's rows: the first request goes out at once (loading.js waits for it before the page shows) ----
  let tags = $state(null);
  let loadError = $state('');
  fetch('/api/tags').then((r) => (r.ok ? r.json() : Promise.reject(new Error(`the app answered ${r.status}`))))
    .then((d) => (tags = d)).catch((e) => (loadError = `Couldn't read the champion tags: ${e.message}`));
  const rows = $derived((tags?.champions ?? []).flatMap((c) => Object.entries(c.identity ?? {}).map(([role, idn]) => ({
    id: `${c.champ_id}-${role}`, champ: c.champ, name: c.name, role: tags.role_labels[role] ?? role,
    style: idn.badge?.text ?? '', range: c.attack_range, ...idn.shares,
  }))));
  const share = (v) => `${Math.round(v * 100)}%`;
  const columns = [
    { key: 'name', label: 'Champion', sortable: true },
    { key: 'role', label: 'Role', sortable: true, width: '96px' },
    { key: 'range', label: 'Range', num: true, sortable: true, width: '88px', title: 'Attack range (Data Dragon)' },
    { key: 'engage', label: 'Engage', num: true, sortable: true, format: share, width: '88px' },
    { key: 'poke', label: 'Poke', num: true, sortable: true, format: share, width: '88px' },
    { key: 'pick', label: 'Pick', num: true, sortable: true, format: share, width: '88px' },
    { key: 'protect', label: 'Protect', num: true, sortable: true, format: share, width: '88px' },
    { key: 'split', label: 'Split', num: true, sortable: true, format: share, width: '88px' },
  ];
  let sort = $state(null);
  let dense = $state(false);
  let picked = $state(null);
  let roleFilter = $state('all');
  let styleFilter = $state(new Set());
  const styles = $derived(Object.values(tags?.styles ?? {}));
  const filtered = $derived(rows.filter((r) => (roleFilter === 'all' || r.role === roleFilter)
    && (!styleFilter.size || r.style.split(' · ').some((s) => styleFilter.has(s)))).slice(0, 14));
  function toggleStyle(s) {
    const next = new Set(styleFilter);
    next.has(s) ? next.delete(s) : next.add(s);
    styleFilter = next;
  }
  let tableState = $state('rows');

  // ---- samples ----
  let mode = $state('plan');
  let side = $state('blue');
  let windowN = $state('20');
  let scope = $state('pool');
  let name = $state('');
  let bracket = $state('gold');
  let games = $state(10);
  let notes = $state('');
  let useDraft = $state(true);
  let weight = $state(0.3);
  let busy = $state(false);
  function pretend() { busy = true; setTimeout(() => (busy = false), 1500); }

  const ALL_NOTICES = [
    { id: 'offline', kind: 'offline', text: 'Reconnecting to the assistant…' },
    { id: 'error', kind: 'error', text: 'The crawl stopped', detail: 'the Riot key was refused (403)', actions: [{ label: 'Open Crawl', href: '#/crawl' }] },
    { id: 'setup', kind: 'setup', text: "Pulling the team's stats: 412 of 1,280 blocks", detail: "Live uses OP.GG's stats until it's done", progress: 412 / 1280 },
    { id: 'key', kind: 'setup', text: 'Add your Riot API key to scout opponents and read match history', actions: [{ label: 'Open Settings', href: '#/settings' }], ondismiss: () => toggleNotice('key') },
    { id: 'update', kind: 'update', text: 'Draft Assistant 0.2.14 is available', detail: 'you have 0.2.12', actions: [{ label: 'Update', onclick: () => {} }] },
    { id: 'fixture', kind: 'fixture', text: 'Demo data: a scripted champ select with synthetic stats' },
  ];
  let noticeOn = $state(new Set(['key', 'update', 'fixture']));
  function toggleNotice(id) {
    const next = new Set(noticeOn);
    next.has(id) ? next.delete(id) : next.add(id);
    noticeOn = next;
  }
  const notices = $derived(ALL_NOTICES.filter((n) => noticeOn.has(n.id)));
</script>

<main class="console">
  <PageHeader title="Kit" description="The shared console components in every state. A working page for the redesign, not part of the app.">
    {#snippet actions()}
      <Segmented label="Mode" bind:value={mode} options={[['plan', 'Plan'], ['tree', 'Matchup tree']]} />
      <Button variant="primary" size="md">Pin for next game</Button>
    {/snippet}
  </PageHeader>

  <Toolbar label="Sample view options">
    <ToolGroup label="Scope"><Segmented label="Scope" bind:value={scope} options={[['pool', 'Our pools'], ['all', 'All champions']]} /></ToolGroup>
    <ToolGroup label="Side"><Segmented label="Side" bind:value={side} options={[['blue', 'Blue'], ['red', 'Red']]} /></ToolGroup>
    <ToolGroup label="Games"><Segmented label="Games" bind:value={windowN} options={[['10', '10'], ['20', '20'], ['all', 'All'], { value: '100', label: '100', disabled: true, title: 'Fewer than 100 games' }]} /></ToolGroup>
    <ToolGroup label="Bracket"><Field label="Bracket" hideLabel bind:value={bracket} options={[['gold', 'Gold'], ['platinum', 'Platinum'], ['emerald', 'Emerald']]} width="120px" /></ToolGroup>
    <ToolGroup><Field type="checkbox" label="Use current draft" bind:checked={useDraft} /></ToolGroup>
    <ToolGroup end><Button variant="ghost">Reset</Button></ToolGroup>
  </Toolbar>

  <div class="cols">
    <section>
      <h2>Button</h2>
      <div class="line"><Button variant="primary">Add player</Button><Button>Run</Button><Button variant="ghost">Cancel</Button>
        <Button danger>Delete…</Button><Button href="#/kit">A link</Button></div>
      <div class="line"><Button variant="primary" disabled>Add player</Button><Button disabled>Run</Button><Button variant="ghost" disabled>Cancel</Button>
        <Button {busy} onclick={pretend}>{busy ? 'Saving…' : 'Save (busy for 1.5 s)'}</Button></div>
      <div class="line"><Button variant="primary" size="md">Pin for next game</Button><Button size="md">Swap</Button><Button variant="ghost" size="md">Bench</Button></div>
      <p class="cap">28px in the console, 32px for a page's main action. One primary per view; a disabled primary is grey.</p>
    </section>

    <section>
      <h2>Segmented</h2>
      <div class="line"><Segmented label="Lens" value="lane" options={[['lane', 'In-lane'], ['fight', 'Teamfight'], ['both', 'Combined']]} />
        <Segmented label="View" value="draft" options={[{ value: 'draft', label: 'Draft', hint: 'D' }, { value: 'strategy', label: 'Strategy', hint: 'S' }]} /></div>
      <div class="line"><Segmented label="Disabled" value="a" disabled options={[['a', 'Blue'], ['b', 'Red']]} /></div>
      <p class="cap">A mode, not an action: never lime. Arrow keys move the choice.</p>
    </section>

    <section>
      <h2>Field</h2>
      <div class="grid3">
        <Field label="Riot ID" bind:value={name} placeholder="Name#TAG" />
        <Field label="Stats bracket" bind:value={bracket} options={[['gold', 'Gold'], ['platinum', 'Platinum'], ['emerald', 'Emerald']]} />
        <Field label="Comfort games cap" type="number" bind:value={games} min="1" max="50" hint="Games after which comfort stops growing" />
        <Field label="Riot ID" value="Nobody#EUW" error="No such account" />
        <Field label="Role" value="Top" disabled />
        <Field label="β (comfort)">
          {#snippet children(id)}<input {id} type="range" min="0" max="2" step="0.05" bind:value={weight} style="flex:1" /><span class="mono val">{Number(weight).toFixed(2)}</span>{/snippet}
        </Field>
      </div>
      <Field label="Paste a pool" type="textarea" bind:value={notes} placeholder={'top: Aatrox (main), Ornn\nmid: Ahri'} mono />
      <div class="line"><Field type="checkbox" label="Use current draft" bind:checked={useDraft} />
        <Field type="checkbox" label="Lane hint on the Roster page" hint="Off by default" checked={false} />
        <Field type="checkbox" label="Scout opponents" checked disabled /></div>
    </section>

    <section>
      <h2>Stat</h2>
      <div class="line stats">
        <Stat value="54" unit="%" label="Win rate" delta={3.2} deltaUnit="pp" />
        <Stat value="31" label="Games" />
        <Stat value="3.4" label="KDA" delta={-0.4} />
        <Stat value={null} label="CS / min" note="Needs 5 games" />
      </div>
      <div class="line stats">
        <Stat size="sm" value="18,204" label="Matches, Gold" />
        <Stat size="sm" value="+0.12" label="Lane execution" note="Wins lane" />
        <Stat size="sm" value="48" unit="MB" label="Databases" delta={0} />
      </div>
      <h2 class="gap">Tag</h2>
      <div class="line"><strong>Ornn</strong><Tag>Engage</Tag><Tag>Front-line</Tag><Tag warn>low data</Tag></div>
      <div class="line"><Tag onclick={() => {}} pressed={false}>Unreviewed</Tag><Tag onclick={() => {}} pressed={true}>Archetype</Tag><Tag warn onclick={() => {}} pressed={false}>Kit differs</Tag></div>
      <p class="cap">Quiet text by default; a border only where the tag is a filter you can click.</p>
    </section>
  </div>

  <section>
    <h2>Notice</h2>
    <div class="line">
      {#each ALL_NOTICES as n}<Tag onclick={() => toggleNotice(n.id)} pressed={noticeOn.has(n.id)}>{n.id}</Tag>{/each}
    </div>
    <div class="noticebox"><Notice items={notices} /></div>
    <div class="line"><span class="label-caps">Live, one line</span><Notice compact items={notices} /></div>
    <p class="cap">One strip, the most urgent first: offline, error, setup, update, demo data. The rest wait behind "+N more".</p>
  </section>

  <section>
    <h2>Table</h2>
    <Toolbar label="Table options">
      <ToolGroup label="Role"><Segmented label="Role" bind:value={roleFilter} options={[['all', 'All'], ...(tags?.roles ?? []).map((r) => [tags.role_labels[r], tags.role_labels[r]])]} /></ToolGroup>
      <ToolGroup label="Style">{#each styles as s}<Tag onclick={() => toggleStyle(s)} pressed={styleFilter.has(s)}>{s}</Tag>{/each}</ToolGroup>
      <ToolGroup label="State"><Segmented label="State" bind:value={tableState} options={[['rows', 'Rows'], ['loading', 'Loading'], ['empty', 'Empty'], ['error', 'Error']]} /></ToolGroup>
      <ToolGroup><Field type="checkbox" label="Dense" bind:checked={dense} /></ToolGroup>
    </Toolbar>
    <Table {columns} rows={tableState === 'rows' ? filtered : []} key={(r) => r.id} {dense} bind:sort
           loading={tableState === 'loading' || (!tags && !loadError)}
           error={tableState === 'error' ? "Couldn't read the champion tags: the app answered 500" : loadError}
           empty="No champion matches these filters" selected={(r) => r.id === picked}
           onrowclick={(r) => (picked = picked === r.id ? null : r.id)}>
      {#snippet cell(row, col, i, text)}
        {#if col.key === 'name'}
          <span class="champcell"><Champ champ={row.champ} size={dense ? 20 : 24} /><strong>{row.name}</strong>
            {#each row.style ? row.style.split(' · ') : [] as s}<Tag>{s}</Tag>{/each}</span>
        {:else}{text(row, col)}{/if}
      {/snippet}
    </Table>
  </section>

  <section>
    <h2>Panel</h2>
    <div class="cols">
      <Panel title="Crawl">
        {#snippet actions()}<Button>Start</Button>{/snippet}
        <div class="grid3">
          <Field label="Patch" value="16.22" options={['16.22', '16.21']} />
          <Field label="Hours" type="number" value={8} />
          <Field label="IDs per queue" type="number" value={100} />
        </div>
        <Panel title="A panel placed inside a panel">
          <p class="cap">It becomes a section under a hairline rule: boxes don't nest.</p>
        </Panel>
      </Panel>
      <Panel title="Table on a panel" flush>
        <Table dense sticky={false} columns={[{ key: 'tier', label: 'Tier' }, { key: 'n', label: 'Matches', num: true, format: (v) => v.toLocaleString() }, { key: 'd', label: 'Today', num: true, format: (v) => `+${v}` }]}
               rows={[{ tier: 'Emerald', n: 20412, d: 1210 }, { tier: 'Platinum', n: 19877, d: 944 }, { tier: 'Gold', n: 18204, d: 1022 }, { tier: 'Silver', n: 12050, d: 310 }]} />
      </Panel>
    </div>
  </section>
</main>

<style>
  section { padding: 20px 0; border-top: 1px solid var(--hairline); min-width: 0; }
  h2 { margin: 0 0 12px; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; }
  h2.gap { margin-top: 24px; }
  .cols { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 32px; }
  section .cols { gap: 16px; }
  @media (max-width: 1180px) { .cols { grid-template-columns: minmax(0, 1fr); } }
  .line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .line.stats { gap: 16px 40px; align-items: flex-start; margin-bottom: 20px; }
  .grid3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px; align-items: start; }
  .cap { margin: 0; font-size: 12px; color: var(--text-3); }
  .val { font-size: 12.5px; color: var(--text-1); width: 36px; text-align: right; }
  .noticebox { margin-bottom: 12px; border: 1px dashed var(--hairline); }
  .champcell { display: inline-flex; align-items: center; gap: 8px; }
  .champcell strong { font-weight: 600; font-size: 14px; }
</style>
