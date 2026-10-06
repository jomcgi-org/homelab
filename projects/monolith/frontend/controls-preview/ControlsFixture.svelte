<script>
  import {
    Breadcrumb,
    Button,
    Disclosure,
    Field,
    PageHeader,
    Tabs,
  } from "@homelab/design-system/components";
  import "@homelab/design-system/tokens/contract.css";
  import "@homelab/design-system/tokens/technical-drawing.css";

  let {
    buttonType,
    orientation = "horizontal",
    headingLevel = 2,
    initialSelected = "overview",
    tabs = [
      { id: "overview", label: "Overview" },
      { id: "unavailable", label: "Unavailable", disabled: true },
      { id: "metrics", label: "Metrics" },
      { id: "logs", label: "Logs" },
    ],
  } = $props();
  const initialSelection = () => initialSelected;
  let selected = $state(initialSelection());
  let changes = $state(0);
  let clicks = $state(0);
  let submissions = $state(0);
  let open = $state(false);
  let fieldDisabled = $state(false);
  const longName =
    "SyntheticReferenceWithAnUnbrokenNameThatMustWrapAtNarrowWidths";
</script>

{#snippet sample(name, theme)}
  <section data-sample={name} data-ds-theme={theme}>
    <PageHeader
      title={longName}
      level={headingLevel}
      description="Synthetic controls only"
    >
      {#snippet breadcrumb()}
        <Breadcrumb
          items={[
            { label: "Home", href: "#home" },
            { label: longName, href: "#current", current: true },
          ]}
        />
      {/snippet}
      {#snippet actions()}
        <Button
          variant="secondary"
          data-action="header"
          onclick={() => clicks++}>Header action</Button
        >
      {/snippet}
    </PageHeader>
    <form
      id={`${name}-form`}
      onsubmit={(event) => {
        event.preventDefault();
        submissions++;
      }}
    >
      <Field
        label="Sample region"
        description="Select a synthetic region"
        error="Choose another region"
        required
        disabled={fieldDisabled}
      >
        {#snippet control(attributes)}
          <select {...attributes} name="region">
            <option value="north">North</option><option value="south"
              >South</option
            >
          </select>
        {/snippet}
      </Field>
      <Field label="Unavailable sample" disabled>
        {#snippet control(attributes)}<input
            {...attributes}
            name="unavailable"
            placeholder="Unavailable sample"
          />{/snippet}
      </Field>
      <div class="controls">
        <Button
          type={buttonType}
          data-action="default"
          name="action"
          value="sample"
          onclick={() => clicks++}>Sample action: {clicks}</Button
        >
        <Button type="submit" variant="secondary" data-action="submit"
          >Submit sample</Button
        >
        <Button type="reset" variant="quiet">Reset sample</Button>
        <Button disabled data-action="disabled" onclick={() => clicks++}
          >Disabled action</Button
        >
        <Button
          aria-label="Named action"
          data-action="named"
          onclick={() => clicks++}
        />
        <Button
          variant="quiet"
          data-action="field"
          onclick={() => (fieldDisabled = !fieldDisabled)}
          >Toggle field disabled</Button
        >
      </div>
    </form>
    <Button
      form={`${name}-form`}
      type="submit"
      variant="secondary"
      data-action="external-submit">External submit</Button
    >
    <p data-state="submissions">Submissions: {submissions}</p>
    <Disclosure bind:open>
      {#snippet summary()}Sample disclosure{/snippet}
      <p>Expanded synthetic information</p>
    </Disclosure>
    <Button
      data-action="disclosure"
      variant="quiet"
      onclick={() => (open = !open)}>Toggle disclosure binding</Button
    >
    <p data-state="disclosure">Open: {open}</p>
    <h3 id={`${name}-tabs-label`}>Sample panels</h3>
    <Tabs
      {tabs}
      labelledby={`${name}-tabs-label`}
      {orientation}
      bind:selected
      onchange={() => changes++}
    >
      {#snippet panel(tab)}<p>{tab.label} panel content</p>{/snippet}
    </Tabs>
    <Button
      data-action="selection"
      variant="quiet"
      onclick={() => (selected = "logs")}>Select logs externally</Button
    >
    <p data-state="selection">Selected: {selected}; changes: {changes}</p>
  </section>
{/snippet}

<main>
  {@render sample("light", "technical-drawing-light")}
  <section data-sample="outer" data-ds-theme="technical-drawing-light">
    {@render sample("nested", "technical-drawing-dark")}
  </section>
  {@render sample("dark", "technical-drawing-dark")}
  <section data-sample="contract">
    <PageHeader title="Contract fallback" />
    <Breadcrumb items={[{ label: "Current page", current: true }]} />
    <Tabs
      label="Standalone single panel"
      tabs={[{ id: "single", label: "Single" }]}
    >
      {#snippet panel(tab)}<p>{tab.label} panel content</p>{/snippet}
    </Tabs>
  </section>
</main>

<style>
  main {
    display: grid;
    gap: var(--ds-space-lg);
    max-width: 70rem;
    margin: auto;
    font-family: var(--ds-font-body);
  }
  section {
    box-sizing: border-box;
    display: grid;
    min-width: 0;
    gap: var(--ds-space-sm);
    padding: var(--ds-space-md);
  }
  form {
    display: grid;
    gap: var(--ds-space-sm);
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: var(--ds-space-xs);
  }
  p,
  h3 {
    margin: 0;
  }
</style>
