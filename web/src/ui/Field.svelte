<script>
  // A labelled control: 28px high on --surface-2 with a hairline border, the label above it. It draws a text or number
  // input, a select (`options`), a textarea or a checkbox; any other control (a slider with its value) goes in as
  // children and gets the same label, hint and error. A tick is light grey, not lime: being on is a state, not a call
  // to act. An error is written out under the control with ⚠.
  // options: [{ value, label, disabled? }], [value, label] pairs or plain values.
  let {
    label = '', type = 'text', value = $bindable(''), checked = $bindable(false), options = null, hint = '', error = '',
    mono = false, width = null, inline = false, hideLabel = false, id = undefined, children = null, ...rest
  } = $props();

  const uid = $props.id();
  const controlId = $derived(id ?? `f-${uid}`);
  const describedBy = $derived(error ? `${controlId}-err` : hint ? `${controlId}-hint` : undefined);
  const items = $derived(options?.map((o) => (Array.isArray(o) ? { value: o[0], label: o[1] } : typeof o === 'object' ? o : { value: o, label: o })));
</script>

{#if type === 'checkbox'}
  <label class="check" class:off={rest.disabled}>
    <input type="checkbox" bind:checked id={controlId} aria-describedby={describedBy} {...rest} />
    <span>{label}{#if hint}<small id="{controlId}-hint">{hint}</small>{/if}</span>
  </label>
{:else}
  <div class="field" class:inline class:bad={error} style:width={width}>
    {#if label}<label for={controlId} class:sr={hideLabel}>{label}</label>{/if}
    {#if children}
      <div class="custom">{@render children(controlId)}</div>
    {:else if items}
      <span class="select"><select bind:value id={controlId} aria-invalid={error ? true : undefined} aria-describedby={describedBy} {...rest}>
        {#each items as o}<option value={o.value} disabled={o.disabled}>{o.label}</option>{/each}
      </select></span>
    {:else if type === 'textarea'}
      <textarea bind:value id={controlId} class:mono aria-invalid={error ? true : undefined} aria-describedby={describedBy} {...rest}></textarea>
    {:else}
      <input {type} bind:value id={controlId} class:mono aria-invalid={error ? true : undefined} aria-describedby={describedBy} {...rest} />
    {/if}
    {#if error}<p class="msg err" id="{controlId}-err" role="alert">⚠ {error}</p>
    {:else if hint}<p class="msg" id="{controlId}-hint">{hint}</p>{/if}
  </div>
{/if}

<style>
  .field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .field.inline { flex-direction: row; align-items: center; gap: 8px; }
  label { font-size: 12px; color: var(--text-3); }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }

  input, select, textarea { width: 100%; height: 28px; padding: 0 8px; background: var(--surface-2); color: var(--text-1);
                            border: 1px solid var(--hairline); border-radius: var(--radius-sm); font-size: 13px; }
  textarea { height: auto; min-height: 72px; padding: 6px 8px; line-height: 1.45; resize: vertical; }
  input::placeholder, textarea::placeholder { color: var(--text-3); }
  input:hover, select:hover, textarea:hover { border-color: var(--hairline-strong); }
  /* the focused control is where you act: the lime focus ring, drawn as the border so it doesn't shift the layout */
  input:focus-visible, select:focus-visible, textarea:focus-visible { outline: none; border-color: var(--signal); }
  input:disabled, select:disabled, textarea:disabled { background: var(--surface-1); color: var(--text-3); border-color: var(--hairline); }
  input[type="number"] { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
  .bad input, .bad select, .bad textarea { border-color: var(--error); }

  .select { position: relative; display: block; }
  select { appearance: none; -webkit-appearance: none; padding-right: 26px; }
  .select::after { content: ""; position: absolute; right: 10px; top: 50%; width: 6px; height: 6px; margin-top: -5px;
                   border-right: 1.5px solid var(--text-3); border-bottom: 1.5px solid var(--text-3); transform: rotate(45deg);
                   pointer-events: none; }

  .msg { margin: 0; font-size: 12px; color: var(--text-3); }
  .msg.err { color: var(--error); }
  .custom { display: flex; align-items: center; gap: 8px; min-height: 28px; }

  .check { display: inline-flex; align-items: flex-start; gap: 8px; min-height: 28px; padding: 6px 0; font-size: 13px;
           color: var(--text-1); cursor: pointer; }
  .check.off { color: var(--text-3); cursor: default; }
  .check span { display: flex; flex-direction: column; gap: 2px; line-height: 16px; }
  .check small { font-size: 12px; color: var(--text-3); }
  .check input { appearance: none; -webkit-appearance: none; flex: none; position: relative; width: 16px; height: 16px; padding: 0;
                 margin: 0; background: var(--surface-2); border: 1px solid var(--hairline-strong); border-radius: 3px; cursor: inherit; }
  .check input:checked { background: var(--text-1); border-color: var(--text-1); }
  .check input:checked::after { content: ""; position: absolute; left: 4.5px; top: 1.5px; width: 4px; height: 8px;
                                border-right: 2px solid var(--bg); border-bottom: 2px solid var(--bg); transform: rotate(45deg); }
  .check input:focus-visible { outline: 2px solid var(--signal); outline-offset: 2px; border-color: var(--hairline-strong); }
  .check input:disabled { background: var(--surface-1); border-color: var(--hairline); }
  .check input:disabled:checked { background: var(--text-3); border-color: var(--text-3); }
</style>
