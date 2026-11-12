<script>
  // The console's table: hairline rules between rows, no zebra, small-caps headers, figures right-aligned in mono,
  // 32px rows (28px dense), a header that stays in view.
  //
  // Two ways to use it:
  //   <Table columns={[{ key, label, num?, sortable?, value?, format?, tone?, width?, align?, title? }]} rows={...} />
  //     draws the cells itself; pass a `cell(row, col, i)` snippet to draw some yourself (return nothing for the rest
  //     by checking col.key and falling back to `text(row, col)`, which the snippet gets as its 4th argument).
  //     `tone(value, row)` returns 'up' or 'down' to colour a signed figure (colour rule 3: the text carries + or −).
  //     `limit` shows the first N rows after sorting.
  //   <Table> <thead>…</thead><tbody>…</tbody> </Table>
  //     for tables with their own structure (grouped or expanding rows): the same look, your markup.
  // On a Panel the header and the row hover take the panel's surface (--table-bg / --table-hover).
  let {
    columns = null, rows = [], key = null, dense = false, sticky = true, maxHeight = null, scrollX = false,
    sort = $bindable(null), // { key, dir: 'asc' | 'desc' } or null: the rows' own order
    loading = false, error = '', empty = 'Nothing here yet', caption = '', limit = null,
    selected = null, onrowclick = null, cell = null, children = null,
  } = $props();

  const raw = (row, col) => (col.value ? col.value(row) : row[col.key]);
  const text = (row, col) => {
    const v = raw(row, col);
    if (v == null || v === '') return '—';
    return col.format ? col.format(v, row) : v;
  };
  const shown = $derived.by(() => {
    const col = sort && columns?.find((c) => c.key === sort.key);
    let out = rows;
    if (col) {
      const dir = sort.dir === 'desc' ? -1 : 1;
      out = [...rows].sort((a, b) => {
        const x = raw(a, col), y = raw(b, col);
        if (x == null || y == null) return (x == null) - (y == null); // missing values last, whichever way
        return dir * (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y)));
      });
    }
    return limit == null ? out : out.slice(0, limit);
  });
  const tone = (row, col) => (col.tone ? col.tone(raw(row, col), row) : null);
  // first click: figures from the top, words from A; then the other way; then back to the rows' own order
  function sortBy(col) {
    const first = col.num ? 'desc' : 'asc';
    if (sort?.key !== col.key) sort = { key: col.key, dir: first };
    else if (sort.dir === first) sort = { key: col.key, dir: first === 'asc' ? 'desc' : 'asc' };
    else sort = null;
  }
  const ariaSort = (col) => (sort?.key === col.key ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
  function rowKey(e, row) {
    if (e.target !== e.currentTarget || (e.key !== 'Enter' && e.key !== ' ')) return;
    e.preventDefault();
    onrowclick(row);
  }
</script>

<div class="wrap" class:scroll={maxHeight != null || scrollX} style:max-height={maxHeight}>
  <table class="t" class:dense class:sticky>
    {#if caption}<caption>{caption}</caption>{/if}
    {#if columns}
      <thead>
        <tr>
          {#each columns as col}
            <th scope="col" class:num={col.num} class={col.align} style:width={col.width} title={col.title}
                aria-sort={col.sortable ? ariaSort(col) : undefined}>
              {#if col.sortable}
                <button type="button" onclick={() => sortBy(col)}>{col.label}<span class="arrow mono" aria-hidden="true">{sort?.key === col.key ? (sort.dir === 'asc' ? '↑' : '↓') : ''}</span></button>
              {:else}{col.label}{/if}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#if error}
          <tr class="state"><td colspan={columns.length} class="err"><span role="alert">⚠ {error}</span></td></tr>
        {:else if loading && !rows.length}
          <tr class="state"><td colspan={columns.length} aria-busy="true">Loading…</td></tr>
        {:else}
          {#each shown as row, i (key ? key(row) : row)}
            <tr class:click={onrowclick} class:sel={selected?.(row)} aria-selected={selected ? !!selected(row) : undefined}
                tabindex={onrowclick ? 0 : undefined} onclick={onrowclick ? () => onrowclick(row) : undefined}
                onkeydown={onrowclick ? (e) => rowKey(e, row) : undefined}>
              {#each columns as col}
                <td class:num={col.num} class:mono={col.num || col.mono} class={[col.align, tone(row, col)]}>
                  {#if cell}{@render cell(row, col, i, text)}{:else}{text(row, col)}{/if}
                </td>
              {/each}
            </tr>
          {:else}
            <tr class="state"><td colspan={columns.length}>{empty}</td></tr>
          {/each}
        {/if}
      </tbody>
    {:else}
      {@render children?.()}
    {/if}
  </table>
</div>

<style>
  .wrap { min-width: 0; }
  .wrap.scroll { overflow: auto; }
  .t { width: 100%; border-collapse: collapse; border-spacing: 0; font-size: 13px; }
  caption { caption-side: top; text-align: left; padding: 0 0 8px; font-size: 16px; font-weight: 650; letter-spacing: -0.01em; color: var(--text-1); }

  .t :global(th) { height: 28px; padding: 0 12px; text-align: left; vertical-align: middle; white-space: nowrap;
                   font-size: 11px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-3);
                   background: var(--table-bg, var(--bg)); box-shadow: inset 0 -1px 0 var(--hairline-strong); }
  .t.sticky :global(thead th) { position: sticky; top: var(--table-top, 0); z-index: 1; }
  .t :global(td) { height: 32px; padding: 0 12px; line-height: 18px; vertical-align: middle; color: var(--text-1);
                   border-bottom: 1px solid var(--hairline); }
  .t.dense :global(td) { height: 28px; }
  /* icons and controls in a cell sit on the row's middle, so they don't stretch its line */
  .t :global(td > *) { vertical-align: middle; }
  .t :global(th:first-child), .t :global(td:first-child) { padding-left: 8px; }
  .t :global(th:last-child), .t :global(td:last-child) { padding-right: 8px; }
  .t :global(th.num), .t :global(td.num), .t :global(.right) { text-align: right; }
  .t :global(.center) { text-align: center; }
  .t :global(td.num) { font-size: 12.5px; }
  .t :global(td.up), .t :global(td .up) { color: var(--up-text); }
  .t :global(td.down), .t :global(td .down) { color: var(--down-text); }
  .t :global(tbody tr:hover > td) { background: var(--table-hover, var(--surface-1)); }
  .t :global(tbody tr.sel > td) { background: var(--surface-2); }

  tr.click { cursor: pointer; }
  tr.click:focus-visible { outline: 2px solid var(--signal); outline-offset: -2px; }
  tr.state > td { height: 64px; color: var(--text-3); text-align: left; }
  .t tr.state:hover > td { background: none; }
  tr.state > td.err { color: var(--error); }

  th button { display: inline-flex; align-items: center; gap: 4px; margin: 0; padding: 0; border: 0; background: none;
              font: inherit; letter-spacing: inherit; text-transform: inherit; color: inherit; height: 28px; }
  th.num button { flex-direction: row-reverse; }
  th button:hover, th[aria-sort="ascending"] button, th[aria-sort="descending"] button { color: var(--text-1); filter: none; }
  th button:active { transform: none; }
  .arrow { display: inline-block; width: 8px; font-size: 11px; letter-spacing: 0; }
</style>
