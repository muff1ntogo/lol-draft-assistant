<script>
  // One choice from a short list where each option needs a line of explanation (who comes in, where a player goes).
  // options: [{ value, label, note? }]. The mark is light ink, not lime: a choice made is a state.
  let { options, value = $bindable(), label } = $props();
  const uid = $props.id();
</script>

<div class="list" role="radiogroup" aria-label={label}>
  {#each options as o}
    <label class="choice">
      <input type="radio" name="radio-{uid}" value={o.value} bind:group={value} />
      <span><strong>{o.label}</strong>{#if o.note}<small>{o.note}</small>{/if}</span>
    </label>
  {/each}
</div>

<style>
  .list { border-top: 1px solid var(--hairline); }
  .choice { display: flex; align-items: flex-start; gap: 10px; padding: 10px 4px; border-bottom: 1px solid var(--hairline); cursor: pointer; }
  .choice:hover { background: var(--surface-2); }
  .choice span { display: flex; flex-direction: column; gap: 2px; font-size: 13px; }
  .choice strong { font-weight: 600; }
  .choice small { font-size: 12px; color: var(--text-3); }
  input { appearance: none; -webkit-appearance: none; flex: none; width: 16px; height: 16px; margin: 2px 0 0; border: 1px solid var(--hairline-strong);
          border-radius: 50%; background: var(--surface-2); cursor: inherit; }
  input:checked { border: 5px solid var(--text-1); background: var(--bg); }
  input:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; }
</style>
