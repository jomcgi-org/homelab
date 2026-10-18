<script>
  import { Seo } from "$lib/public/components";
  import ProfileSheet from "$lib/public/components/ProfileSheet.svelte";
  import { LOCATION } from "$lib/public/seo.js";
  import {
    contact,
    name,
    tagline,
    summary,
    jobs,
    earlierCareer,
    personalIntro,
    projects,
    skills,
  } from "./cv-data.js";

  // CV data carries only emphasis and links. Preserve its text and link targets
  // without introducing a second markdown renderer or changing its provenance.
  function tokenize(text) {
    const tokens = [];
    const re = /\*\*(.+?)\*\*|\[(.+?)\]\((.+?)\)/g;
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last)
        tokens.push({ type: "text", text: text.slice(last, m.index) });
      if (m[1] !== undefined) tokens.push({ type: "strong", text: m[1] });
      else tokens.push({ type: "link", text: m[2], href: m[3] });
      last = re.lastIndex;
    }
    if (last < text.length)
      tokens.push({ type: "text", text: text.slice(last) });
    return tokens;
  }

  const identity = [
    "Senior Platform Engineer @ Semgrep",
    "AWS / EKS",
    "eBPF & Cilium",
    "Distributed Systems",
    "OpenTelemetry Contributor",
    "K3S Homelab",
    `${LOCATION.short}, ${LOCATION.country}`,
  ];
  const sections = [
    { id: "profile", label: "Profile" },
    { id: "experience", label: "Experience" },
    { id: "personal", label: "Personal engineering" },
    { id: "expertise", label: "Expertise" },
  ];
</script>

<Seo
  title="Joe McGinley · CV"
  description="Joe McGinley, Senior Platform Engineer @ Semgrep. AWS · EKS · Kubernetes · eBPF · Reliability & Observability."
  path="/cv"
  type="profile"
/>

{#snippet inline(text)}
  {#each tokenize(text) as tok}
    {#if tok.type === "strong"}<strong>{tok.text}</strong
      >{:else if tok.type === "link"}<a
        href={tok.href}
        target="_blank"
        rel="noreferrer">{tok.text}</a
      >{:else}{tok.text}{/if}
  {/each}
{/snippet}

<ProfileSheet here="cv" {sections}>
  <header class="edition" id="profile">
    <div class="ed-head">
      Curriculum Vitae <span class="end">{contact.location}</span>
    </div>
    <div class="ed-lead">
      <h1>{name}</h1>
      <p>{tagline}</p>
    </div>
    <div class="ed-foot">
      <a href={`mailto:${contact.email}`}>{contact.email}</a>
      <a href={contact.linkedin.href} target="_blank" rel="noreferrer"
        >{contact.linkedin.label}</a
      >
      <a href={contact.github.href} target="_blank" rel="noreferrer"
        >{contact.github.label}</a
      >
    </div>
    <div class="ed-body prose">
      <h2>Profile</h2>
      <p>{@render inline(summary)}</p>
    </div>
    <ul class="divided" aria-label="Background">
      {#each identity as item}<li>{item}</li>{/each}
    </ul>
  </header>

  <section class="edition" id="experience" aria-labelledby="experience-heading">
    <div class="ed-head"><span class="num">01</span> Work Experience</div>
    <div class="ed-lead"><h2 id="experience-heading">Work Experience</h2></div>
    {#each jobs as job}
      <article class="job">
        <div class="ed-head">
          <h3>{job.company}</h3>
          <span class="end">{job.dates}</span>
        </div>
        <dl class="spec">
          <div class="row">
            <dt>Role</dt>
            <dd>{job.title}</dd>
          </div>
        </dl>
        <div class="ed-body prose">
          {#if job.blurb}<p>{@render inline(job.blurb)}</p>{/if}
          {#if job.bullets}<ul>
              {#each job.bullets as bullet}<li>
                  {@render inline(bullet)}
                </li>{/each}
            </ul>{/if}
          {#if job.highlights}
            {#each job.highlights as highlight}
              <h4>{highlight.title}</h4>
              {#if highlight.intro}<p>{@render inline(highlight.intro)}</p>{/if}
              <ul>
                {#each highlight.bullets as bullet}<li>
                    {@render inline(bullet)}
                  </li>{/each}
              </ul>
            {/each}
          {/if}
        </div>
      </article>
    {/each}
    <div class="ed-body prose">
      <h3>Earlier Career</h3>
      <p>{@render inline(earlierCareer)}</p>
    </div>
  </section>

  <section class="edition" id="personal" aria-labelledby="personal-heading">
    <div class="ed-head">
      <span class="num">02</span> Personal Engineering
      <span class="end">Homelab</span>
    </div>
    <div class="ed-lead">
      <h2 id="personal-heading">Personal Engineering</h2>
      <p>{@render inline(personalIntro)}</p>
    </div>
    <div class="ed-body prose">
      <ul>
        {#each projects as project}<li>{@render inline(project)}</li>{/each}
      </ul>
    </div>
  </section>

  <section class="edition" id="expertise" aria-labelledby="expertise-heading">
    <div class="ed-head"><span class="num">03</span> Technical Expertise</div>
    <div class="ed-lead">
      <h2 id="expertise-heading">Technical Expertise</h2>
    </div>
    <dl class="spec">
      {#each skills as category}
        <div class="row">
          <dt>{category.label}</dt>
          <dd>
            <ul class="divided">
              {#each category.items as item}<li>{item}</li>{/each}
            </ul>
          </dd>
        </div>
      {/each}
    </dl>
  </section>
</ProfileSheet>

<style>
  .job > .ed-head {
    border-top: 1px solid var(--stroke);
  }
  .job h3 {
    margin: 0;
    font: inherit;
    font-weight: 600;
    color: var(--ink);
  }
  .job .spec {
    border-top: 0;
  }
  .spec dd:has(.divided) {
    padding: 0;
  }
  .spec .divided {
    border: 0;
  }
  .spec .divided li {
    font-size: 0.8rem;
  }
</style>
