<script>
  // Synergy v2 rules (docs/synergy-v2.md, Stage 2). Written rules with a prior effect, fitted to crawled data
  // (ridge regression centred on the priors). Flagged = the data disagrees: more than 1.5pp from the prior,
  // or the other sign. Editing a prior writes data/style_rules.yaml or data/team_rules.yaml and refits.
  import { patchLabel } from './patch.js';
import PageHeader from './ui/PageHeader.svelte';
import Toolbar from './ui/Toolbar.svelte';
import ToolGroup from './ui/ToolGroup.svelte';
import Segmented from './ui/Segmented.svelte';
import Field from './ui/Field.svelte';
import Button from './ui/Button.svelte';
import Table from './ui/Table.svelte';
import Tag from './ui/Tag.svelte';

  const PAIRS = ['TOP-JG', 'JG-MID', 'ADC-SUP'];
  let data = $state(null);
  let tab = $state('style');
  let pairFilter = $state('all');
  let onlyFlagged = $state(false);
  let edits = $state({});
  let error = $state({});
  let busy = $state(false);
let loadError = $state('');

async function load() {
 try {
  const res = await fetch('/api/rules');
  if (!res.ok) throw new Error(`request failed (${res.status})`);
  data = await res.json();
  loadError = '';
 } catch (e) { loadError = `Couldn't load rules: ${e.message}`; }
}
  load();

  const fmt = (v) => (v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}`);
  const when = (r) => Object.entries(r.when).map(([role, t]) => `${role} ${t}`).join(' × ');
  function spec(f) {
    if (!f) return '';
    if (f.all) return f.all.map(spec).join(' × ');
    if (f.special) return f.special.replaceAll('_', ' ');
    if (f.protect) return `${f.protect} protected by ${f.by.join(' / ')}`;
    if (f.sum) return `amount of ${f.sum} (full at ${f.cap})`;
    if (f.lack) return `shortfall of ${f.lack} (below ${f.need})`;
    if (f.excess) return `${f.excess} beyond ${f.over}`;
    if (f.max) return `best ${f.max}`;
    if (f.mean) return `average ${f.mean}`;
    return JSON.stringify(f);
  }
  const rows = $derived.by(() => {
    if (!data) return [];
    let list = tab === 'style' ? data.style : data.team;
    if (tab === 'style' && pairFilter !== 'all') list = list.filter((r) => r.pair === pairFilter);
    if (onlyFlagged) list = list.filter((r) => r.flagged);
    return list;
  });
  const flaggedCount = (list) => list.filter((r) => r.flagged).length;

  async function save(r) {
    const value = Number(edits[r.id]);
    if (!Number.isFinite(value)) return;
    busy = true;
    const res = await fetch('/api/rules', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: tab, id: r.id, prior_pp: value }),
    });
    const body = await res.json();
    busy = false;
    if (!res.ok) { error[r.id] = body.detail; return; }
    delete error[r.id];
    delete edits[r.id];
    await load(); // every rule was refitted
  }
</script>

<main class="console">
<PageHeader title="Rules" description="Written style and team priors, compared with the fit from crawled matches.">
 {#snippet actions()}<Button variant="ghost" href="#/settings">Settings</Button>{/snippet}
</PageHeader>
  {#if data}
<Toolbar label="Rule filters">
<ToolGroup label="Rules"><Segmented label="Rule group" options={[["style", `Pair style (${data.style.length})`], ["team", `Team (${data.team.length})`]]} bind:value={tab} /></ToolGroup>
      {#if tab === 'style'}
<ToolGroup label="Pair"><Segmented label="Lane pair" options={[["all", "All pairs"], ...PAIRS.map((p) => [p, p])]} bind:value={pairFilter} /></ToolGroup>
      {/if}
<ToolGroup><Field type="checkbox" label={`Flagged only (${flaggedCount(tab === 'style' ? data.style : data.team)})`} bind:checked={onlyFlagged} /></ToolGroup>
<ToolGroup end><span class="window mono">Window {patchLabel(data.window) ?? '—'} · {data.matches.toLocaleString()} matches · prior SD {data.prior_sd_pp}pp</span></ToolGroup>
</Toolbar>
<p class="help">
      Effects are in percentage points at full strength (both traits at maximum). The fit starts from the prior and
      moves only as far as the data supports, so rules with little support stay near what's written.
      {#if tab === 'style'}Support = games of champion pairs the rule applies to.{:else}Support = matches where the
      rule applied to either team. Cross rules multiply our feature by theirs.{/if}
    </p>
<Table sticky={false} scrollX={true}>
      <thead><tr>
        {#if tab === 'style'}<th>Pair</th><th>When</th>{:else}<th>Rule</th><th>Feature</th>{/if}
        <th>Note</th><th class="num">Prior</th><th class="num">Fitted</th><th class="num">Support</th><th></th>
      </tr></thead>
      <tbody>
        {#each rows as r (r.id)}
          <tr class:flagged={r.flagged}>
            {#if tab === 'style'}
              <td class="nowrap">{r.pair}</td><td class="mono">{when(r)}</td>
            {:else}
<td class="mono">{r.id}{#if r.cross}<Tag>vs</Tag>{/if}</td>
              <td class="small">{#if r.cross}ours: {spec(r.ours)} × theirs: {spec(r.theirs)}{:else}{spec(r.feature)}{/if}</td>
            {/if}
            <td>{r.note}</td>
            <td class="num">
              <input type="number" step="0.1" value={edits[r.id] ?? r.prior_pp}
                     oninput={(e) => (edits[r.id] = e.target.value)} />
            </td>
<td class="num" class:up={r.fitted_pp > 0} class:down={r.fitted_pp < 0}>{fmt(r.fitted_pp)}</td>
            <td class="num">{r.support.toLocaleString()}</td>
            <td class="nowrap">
{#if r.flagged}<Tag warn title="More than 1.5pp from the prior, or the other sign">disagrees</Tag>{/if}
              {#if edits[r.id] != null && Number(edits[r.id]) !== r.prior_pp}
<Button variant="primary" disabled={busy} onclick={() => save(r)}>Save</Button>
              {/if}
              {#if error[r.id]}<div class="err">{error[r.id]}</div>{/if}
            </td>
          </tr>
        {:else}
<tr><td colspan="7" class="quiet">Nothing matches these filters.</td></tr>
        {/each}
      </tbody>
</Table>
{:else if loadError}
<p class="message error" role="alert">⚠ {loadError} <Button variant="ghost" onclick={load}>Try again</Button></p>
{:else}
<p class="message" aria-busy="true">Loading rules…</p>
  {/if}
</main>

<style>
.window { color: var(--text-3); font-size: 12px; white-space: nowrap; }
.help { max-width: 94ch; margin: 0 0 16px; color: var(--text-3); font-size: 12px; line-height: 1.55; }
input[type='number'] { width: 72px; height: 24px; padding: 0 6px; text-align: right; background: var(--surface-2); color: var(--text-1); border: 1px solid var(--hairline); border-radius: var(--radius-sm); font: 12px var(--font-mono); }
input[type='number']:hover { border-color: var(--hairline-strong); }
input[type='number']:focus-visible { outline: none; border-color: var(--signal); }
tr.flagged :global(td) { background: var(--surface-1); }
  .nowrap { white-space: nowrap; }
.err, .error { color: var(--error); font-size: 12px; }
.quiet, .message { color: var(--text-3); }
.message { margin: 16px 0; }
  .small { font-size: 12px; }
</style>
