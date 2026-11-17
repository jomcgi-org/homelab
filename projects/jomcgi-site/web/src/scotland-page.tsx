import { Heading } from "@astryxdesign/core/Heading";
import { VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { Fragment } from "react";
import { Shell } from "./Shell";
import { policyLinks } from "./policy-data";
import {
  METHOD_URL,
  SPEC_URL,
  curl,
  facts,
  listings,
  setups,
  sources,
} from "./scotland-data";
import { ORIGIN } from "./sections";
import "./scotland.css";

export function ScotlandPage() {
  return (
    <Shell>
      <VStack gap={8}>
        <nav aria-label="Sections">
          <Text type="body">/ Index</Text>
          <ul className="scotland-links">
            <li>
              <a href="#connect">01 Connect</a>
            </li>
            <li>
              <a href="#api">02 API</a>
            </li>
            <li>
              <a href="#data">03 Data</a>
            </li>
          </ul>
        </nav>

        <section className="scotland-section" aria-labelledby="intro-h">
          <div className="scotland-band">
            <span>Scotland connector</span>
            <span>v1 · read-only</span>
          </div>
          <VStack gap={3}>
            <Heading level={1} id="intro-h">
              Ask your assistant what to do outdoors, and whether it is on.
            </Heading>
            <Text type="body">
              Plan: give it a place in Scotland and a few days, and for each day
              it suggests the Walkhighlands walks that fit the weather and the
              best nearby dark-sky site for the night, each with go or marginal
              and the reason, plus sunrise, sunset and the moon.
            </Text>
            <Text type="body">
              Check: name a walk and a day, or a place and a night, and it says
              whether it is on, when to start, which other days suit it and what
              is better nearby. Each walk links to its route on Walkhighlands.
            </Text>
            <dl className="scotland-spec">
              {facts.map((fact) => (
                <div key={fact.k}>
                  <dt>{fact.k}</dt>
                  <dd>{fact.code ? <code>{fact.v}</code> : fact.v}</dd>
                </div>
              ))}
            </dl>
            <a href={`${ORIGIN}/app/stars`}>prefer a map? the stars app</a>
          </VStack>
        </section>

        <section
          className="scotland-section"
          id="connect"
          aria-labelledby="connect-h"
        >
          <div className="scotland-band">
            <span>01</span>
            <span>Connect</span>
          </div>
          <VStack gap={3}>
            <Heading level={2} id="connect-h">
              Add it to your assistant
            </Heading>
            <Text type="body">
              It is a remote MCP server: one URL, no account.
            </Text>
            {setups.map((setup) => (
              <Fragment key={setup.id}>
                <Heading level={3}>{setup.label}</Heading>
                <Text type="body">{setup.how}</Text>
                {setup.code && (
                  <pre className="scotland-code" data-lang={setup.lang}>
                    <code>{setup.code}</code>
                  </pre>
                )}
              </Fragment>
            ))}
            <ul className="scotland-links" aria-label="Listings">
              {listings.map((listing) => (
                <li key={listing.label}>
                  {listing.href ? (
                    <a href={listing.href} target="_blank" rel="noreferrer">
                      {listing.label} ↗
                    </a>
                  ) : (
                    <>
                      <span>{listing.label}</span> <small>soon</small>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </VStack>
        </section>

        <section className="scotland-section" id="api" aria-labelledby="api-h">
          <div className="scotland-band">
            <span>02</span>
            <span>API</span>
          </div>
          <VStack gap={3}>
            <Heading level={2} id="api-h">
              Or call it over HTTPS
            </Heading>
            <Text type="body">
              The same answers as JSON, cached at the edge. Every response
              carries when the forecast was fetched and the sources to credit.
            </Text>
            <pre className="scotland-code" data-lang="sh">
              <code>{curl}</code>
            </pre>
            <ul className="scotland-links">
              <li>
                <a href={SPEC_URL}>OpenAPI spec</a>
              </li>
              <li>
                <a href={METHOD_URL}>method</a>
              </li>
            </ul>
          </VStack>
        </section>

        <section
          className="scotland-section"
          id="data"
          aria-labelledby="data-h"
        >
          <div className="scotland-band">
            <span>03</span>
            <span>Data</span>
          </div>
          <VStack gap={3}>
            <Heading level={2} id="data-h">
              Where the answers come from
            </Heading>
            <dl className="scotland-spec">
              {sources.map((source) => (
                <div key={source.name}>
                  <dt>{source.name}</dt>
                  <dd>
                    {source.use} · {source.licence}
                  </dd>
                </div>
              ))}
            </dl>
            <ul className="scotland-links">
              {policyLinks.map((link) => (
                <li key={link.href}>
                  <a href={link.href}>{link.label}</a>
                </li>
              ))}
            </ul>
          </VStack>
        </section>
      </VStack>
    </Shell>
  );
}
