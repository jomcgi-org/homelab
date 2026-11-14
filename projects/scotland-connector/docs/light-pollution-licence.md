# Light-pollution rights remain unresolved

Checked 13 November 2026 for [#7253](https://github.com/jomcgi-org/homelab/issues/7253).
Commercial-use permission and the required attribution for the actual sampled
RGB raster remain unverified. No source-specific licence URL has been
established for that input. Joe's 20 October 2026 decision keeps this follow-up
non-blocking for launch and public promotion.

## Historical input has a Lorenz lead

At [668072e9be64ccf34acfeea042e45dbe0a65f510](https://github.com/jomcgi-org/homelab/tree/668072e9be64ccf34acfeea042e45dbe0a65f510/projects/stargazer/backend),
`config.py` defaults to David Lorenz's
[Europe2024.png](https://djlorenz.github.io/astronomy/lp2024/Europe2024.png).
`acquisition.py` downloads it, but skips an existing file. Settings can override
the URL. `preprocessing.py` assigns EPSG:4326 and Europe bounds, expands the
palette to RGB, then crops with GDAL to Scotland as `scotland_lp_2024.tif`.
It also skips an existing output. This establishes the intended adaptation,
without proving the bytes of the raster used for site selection.

The generator introduced in
[e5d21884ad918e02ffd8de150eab6c937780fab7](https://github.com/jomcgi-org/homelab/blob/e5d21884ad918e02ffd8de150eab6c937780fab7/projects/monolith/stars/grid_gen/generate_grid_v2.py)
instructs copying the processed raster from stargazer. It samples an eight-stop
RGB legend, retains its three darkest classes and warns that the PVC's palette
is mismatched. [PR #2595](https://github.com/jomcgi-org/homelab/pull/2595)
describes the local raster and grid rebuild without recording an upstream URL
or input digest. [PR #2702](https://github.com/jomcgi-org/homelab/pull/2702)
lists raw and processed files archived before retirement. Its verification
describes matching object lengths; it supplies no source-to-grid digest chain.
The current job accepts operator-selected objects and records no publisher in
the site rows. A filename, archive listing or palette cannot settle provenance.

## Public terms cover different products

- **Lorenz 2024 Europe render, candidate only.** The public PNG exists in
  [the publisher's repository](https://github.com/djlorenz/djlorenz.github.io/tree/afb94a2627325fc0915b66240050d67b95d12a74/astronomy/lp2024).
  The [atlas page](https://djlorenz.github.io/astronomy/lp/) now describes a
  2025 model using EOG annual nighttime lights and NOAA VIIRS inputs. It asks
  users to avoid equating zenith brightness with Bortle classes. That page,
  its [colour explanation](https://djlorenz.github.io/astronomy/lp/colors.html)
  and the repository README yielded no explicit commercial-use grant or
  attribution licence. The recursive repository tree had no licence-named
  file, and GitHub's repository licence endpoint returned 404. These bounded
  checks do not prove that permission is unavailable elsewhere. Terms for
  satellite inputs do not establish rights to Lorenz's model output or render.
- **Falchi et al. 2016 dataset, unconfirmed input.** The authoritative
  [GFZ record, DOI 10.5880/GFZ.1.4.2016.001](https://doi.org/10.5880/GFZ.1.4.2016.001)
  identifies CC BY-NC 4.0 and records the licence change on 13 November 2019.
  The [licence](https://creativecommons.org/licenses/by-nc/4.0/) permits
  sharing and adaptation for non-commercial purposes, requires credit,
  a licence link and indication of modifications, and prohibits added legal
  or technical restrictions. It supplies no commercial-use permission.
- **lightpollutionmap.info renders and tiles, unconfirmed input.** The
  [publisher's help, FAQ 14, 29 and 30](https://www2.lightpollutionmap.info/help.html)
  distinguishes downloadable rendered RGB maps from original World Atlas
  data. It requests credit to Jurij Stare and the website, with the Falchi
  dataset and paper citations when World Atlas data is displayed. Its link
  to the original dataset's CC BY-NC terms does not establish commercial
  rights to a particular rendered download or tile adaptation.

The [1 November issue memo](https://github.com/jomcgi-org/homelab/issues/7253#issuecomment-6571118221)
identifies Falchi by palette similarity while acknowledging unknown origin.
The [12 November free-only decision](https://github.com/jomcgi-org/homelab/issues/7253#issuecomment-6838249855)
does not provide provenance or a rights-holder grant. Free access alone does
not establish compliance with the licence's commercial-purpose restriction.

## Evidence needed to finish attribution

Resume with a recorded link from the raster actually sampled for the loaded
grid to its upstream product and version, including any crop, reprojection,
rendering or tile assembly. Obtain authoritative terms or existing written
permission covering that exact render, derived site selection and the intended
commercial use, with its required credit and notices. This investigation used
repository history, public issue/PR evidence and public publisher pages. It did
not read private objects, live secrets or cluster state, contact publishers,
accept terms or regenerate the grid.

After permission is established, cover `Plan` with stargazing, `Check` with
stargazing items and `Light` when a grid site is selected. Their current source
assembly is in `plan.go`, `check.go` and `light.go`. Walk-only answers, `Light`
without a site and the separate aurora snapshot do not use this site grid.
Existing real-response fixtures in `connector_test.go` can verify conditional
metadata and preservation of the other sources. No runtime change or rollout
is part of this evidence-only delivery.
