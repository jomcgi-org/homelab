---
title: Stars: Dark-Sky Windows
date: 2026-08-22
summary: Finds the best dark-sky viewing windows across Scotland: scores each site's upcoming hours for darkness and clear sky, then serves the ones that qualify.
public: false
---

Finding a good stargazing night means combining how dark a site is with whether the sky will actually be clear once it gets dark. Stars pairs a light-pollution grid of road-accessible dark sites with rolling weather forecasts and surfaces the upcoming hours that are both dark and clear.

## How it works

**Site grid.** A light-pollution grid of ~14k road-accessible dark sites is built offline and uploaded to SeaweedFS. A scheduled job wholesale-replaces the stars.sites table.

**Forecast scoring.** An hourly job fetches MET Norway forecasts for every site and scores each future hour for darkness (sun below the threshold, astronomy via astral) and clear sky (cloud below the threshold).

**Metric.** The unit is clear-dark hours. Qualifying hours land in Postgres (stars.site_hours); an hourly prune drops hours once their clock hour has elapsed.

**Delivery.** A wholly public, read-only domain folded into the monolith: a slim SSR payload lists sites with their upcoming windows, the per-site history graph loads lazily, and the page is edge-cached.

## Source

- [Live at jomcgi.dev/app/stars](https://jomcgi.dev/app/stars)

<!-- Numbers above were current on 2026-08-22 when this was transcribed from the engineering page. This is a point-in-time post; do not update it, write a new one. -->
