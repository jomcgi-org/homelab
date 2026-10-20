// The homepage portrait: a greyscale cut-out printed in the sheet's own
// tones, cut into vertical strips, drawn behind a perforated plate. It only
// holds together while the visitor is still. Moving fast scatters it,
// ignoring it lets it drift, leaving the window makes it come apart, and a
// glitch clock cuts into it on a random schedule that quickens the longer
// nobody is doing anything. Nothing here touches layout: the canvas is a
// fixed box and every effect is drawn inside it.
//
// The pure parts (the tone ramps, the LUT, the glitch schedule) are exported
// so they can be asserted without a canvas; createPortrait wires them to a
// canvas and the window and returns a destroy function.

/** Five-stop ramps from the darkest tone to the sheet, per scheme. The dark
 *  end is near-ink so shadows print as ink; the light end is the sheet so
 *  highlights melt into the page in both schemes. */
export const RAMPS = {
  light: [
    [0.082, 0.086, 0.094],
    [0.29, 0.29, 0.282],
    [0.557, 0.553, 0.529],
    [0.788, 0.78, 0.753],
    [0.953, 0.949, 0.933],
  ],
  dark: [
    [0.059, 0.063, 0.071],
    [0.235, 0.239, 0.255],
    [0.49, 0.494, 0.471],
    [0.722, 0.718, 0.69],
    [0.922, 0.918, 0.894],
  ],
};

/** The inks a bored portrait re-prints in: the technical-drawing figure
 *  tones plus the sheet's ok green, each as [deep shadow, ink]. */
export const INKS = {
  blue: ["#061d4d", "#0b6bff"],
  red: ["#4a0b0c", "#d0262a"],
  magenta: ["#4a0040", "#d100b5"],
  green: ["#0b3d1e", "#3d8f5f"],
  warm: ["#4d3a00", "#e0a800"],
};

export const STRIPS = 56;

/** Idle thresholds, in seconds, for the glitch clock and the drift. */
export const RESTLESS_AFTER_S = 3;
export const BORED_AFTER_S = 30;

export function hexToRgb(hex) {
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
}

/** A 256-entry RGB lookup table (as a flat Uint8ClampedArray of 768) that
 *  maps a grey level onto a five-stop ramp by linear interpolation. */
export function buildLut(ramp) {
  const lut = new Uint8ClampedArray(256 * 3);
  for (let v = 0; v < 256; v++) {
    const t = (v / 255) * 4;
    const k = Math.min(3, Math.floor(t));
    const f = t - k;
    for (let c = 0; c < 3; c++) {
      lut[v * 3 + c] = (ramp[k][c] + (ramp[k + 1][c] - ramp[k][c]) * f) * 255;
    }
  }
  return lut;
}

/** Recolours RGBA pixel data in place: the red channel (the grey level) is
 *  looked up in the LUT and written to all three channels. */
export function applyLut(pixels, lut) {
  for (let i = 0; i < pixels.length; i += 4) {
    const v = pixels[i];
    pixels[i] = lut[v * 3];
    pixels[i + 1] = lut[v * 3 + 1];
    pixels[i + 2] = lut[v * 3 + 2];
  }
  return pixels;
}

/** A ramp whose dark end is one of the inks and whose light end is the
 *  scheme's own, so a re-inked print still melts into the sheet. */
export function inkRamp(scheme, ink) {
  const base = RAMPS[scheme];
  return [
    hexToRgb(INKS[ink][0]),
    hexToRgb(INKS[ink][1]),
    base[2],
    base[3],
    base[4],
  ];
}

const POOLS = {
  calm: ["freeze", "tear"],
  restless: ["tear", "freeze", "flash"],
  bored: ["tear", "band", "tint", "shuffle", "band", "flash", "tint", "freeze"],
  gone: ["tear", "shuffle", "freeze", "flash", "tint", "band"],
};

/** Which glitches may fire and how often, from what the visitor is doing.
 *  `mean` is the mean gap in milliseconds; gaps are drawn from an
 *  exponential distribution so the clock never settles into a rhythm. */
export function glitchSchedule({ idle, gone, hidden }) {
  if (gone || hidden) return { pool: POOLS.gone, mean: 350 };
  if (idle > BORED_AFTER_S) return { pool: POOLS.bored, mean: 220 };
  if (idle > RESTLESS_AFTER_S) return { pool: POOLS.restless, mean: 1800 };
  return { pool: POOLS.calm, mean: 9000 };
}

/** The next gap from an exponential distribution with the given mean, plus a
 *  floor so two cuts never land in the same frame. */
export function nextGap(mean, random = Math.random) {
  return -Math.log(1 - random()) * mean + 60;
}

/** How far the strips drift apart from being ignored, in pixels: nothing
 *  for the first five seconds, then widening, with a higher ceiling once
 *  the visitor is past the bored threshold. */
export function driftAmplitude(idle) {
  const cap = idle > BORED_AFTER_S ? 70 : 30;
  return Math.max(0, Math.min(cap, (idle - 5) * 4));
}

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const rnd = (a, b) => a + Math.random() * (b - a);
const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

/**
 * Wires the portrait to a canvas. `host` is the element the pointer is
 * measured against (the canvas's box); `src` is the grey cut-out, a PNG with
 * the figure on transparency; `scheme()` returns "light" or "dark" for the
 * current paint; `demo()` returns "", "bored" or "gone" to force a state.
 */
export function createPortrait({
  canvas,
  host,
  src,
  scheme,
  reduceMotion = false,
  demo = () => "",
}) {
  const ctx = canvas.getContext && canvas.getContext("2d");
  if (!ctx) return { destroy() {} };

  const img = new Image();
  const toned = document.createElement("canvas");
  const tctx = toned.getContext("2d");
  const altCache = new Map();
  let tonedFor = "";
  let alt = null;
  let altUntil = 0;
  let band = null;
  let freezeUntil = 0;
  let scatter = 0;
  let W = 0;
  let H = 0;
  let raf = 0;
  let glitchTimer = 0;
  let alive = true;

  const strips = Array.from({ length: STRIPS }, (_, i) => ({
    dx: 0,
    dy: 0,
    vx: 0,
    vy: 0,
    r: (Math.sin(i * 12.9898) * 43758.5453) % 1,
    hold: 0,
    hx: 0,
    hy: 0,
  }));

  // What the visitor is doing, measured on the window.
  const V = {
    x: -9999,
    y: -9999,
    speed: 0,
    lastMove: Date.now(),
    gone: false,
    goneAt: 0,
    touch: false,
    scrollV: 0,
    lastScroll: window.scrollY,
    lastScrollT: Date.now(),
  };
  let px0 = 0;
  let py0 = 0;
  let pt0 = Date.now();

  function recolour(target, ramp) {
    if (!img.naturalWidth) return false;
    target.width = img.naturalWidth;
    target.height = img.naturalHeight;
    const c = target.getContext("2d");
    c.drawImage(img, 0, 0);
    let data;
    try {
      data = c.getImageData(0, 0, target.width, target.height);
    } catch {
      return false;
    }
    applyLut(data.data, buildLut(ramp));
    c.putImageData(data, 0, 0);
    return true;
  }

  function tone() {
    if (!img.complete || !img.naturalWidth) return;
    const key = scheme();
    if (tonedFor === key) return;
    if (recolour(toned, RAMPS[key])) tonedFor = key;
  }

  function reink(name) {
    const key = `${name}:${tonedFor}`;
    if (altCache.has(key)) return altCache.get(key);
    if (!tonedFor) return null;
    const c = document.createElement("canvas");
    if (!recolour(c, inkRamp(tonedFor, name))) return null;
    altCache.set(key, c);
    return c;
  }

  function size() {
    const r = host.getBoundingClientRect();
    if (!r.width) return;
    const d = Math.min(2, window.devicePixelRatio || 1);
    W = r.width;
    H = r.height;
    canvas.width = W * d;
    canvas.height = H * d;
    ctx.setTransform(d, 0, 0, d, 0, 0);
  }

  function state() {
    const mode = demo();
    const idle = mode === "bored" ? 999 : (Date.now() - V.lastMove) / 1000;
    const gone = V.gone || mode === "gone";
    return { idle, gone };
  }

  function draw(t) {
    if (!W) size();
    if (!img.complete || !W) return;
    tone();
    const r = host.getBoundingClientRect();
    const px = V.x - r.left;
    const py = V.y - r.top;
    const over = px > -40 && px < W + 40 && py > -40 && py < H + 40;
    const { idle, gone } = state();
    const shove = clamp(V.speed / 700, 0, 1);

    if (!reduceMotion) {
      scatter = Math.max(
        scatter * 0.9,
        clamp((V.speed - 250) / 25, 0, 60),
        clamp(Math.abs(V.scrollV) / 60, 0, 50),
      );
    }
    V.speed *= 0.94;
    V.scrollV *= 0.85;
    const drift = reduceMotion ? 0 : driftAmplitude(idle);
    const sw = W / STRIPS;
    const now = Date.now();

    for (let i = 0; i < STRIPS; i++) {
      const s = strips[i];
      const u = i / (STRIPS - 1);
      const cx = (i + 0.5) * sw;
      let tx = 0;
      let ty = 0;
      if (!reduceMotion) {
        // tilt toward the pointer
        ty +=
          Math.sin(u * Math.PI * 2 + (px / W) * Math.PI) *
          5 *
          clamp(py / H - 0.5, -1, 1) *
          (over ? 1 : 0.2);
        // too fast: scatter
        ty += (s.r - 0.5) * 2 * scatter;
        tx += Math.sin(i * 7.3) * 0.5 * scatter;
        // ignored: drift apart
        ty += Math.sin(t / 900 + i * 0.6) * drift;
        // shoved aside, harder the faster the pointer moves
        if (over && py > -60 && py < H + 60) {
          const d = cx - px;
          const R = 90 + 120 * shove;
          if (Math.abs(d) < R) {
            const f = Math.pow(1 - Math.abs(d) / R, 1.6);
            tx += Math.sign(d || 1) * f * (28 + 110 * shove);
            ty += (py < H / 2 ? 1 : -1) * f * 40 * shove;
          }
        }
        // not looking: it comes apart, faster the longer you are gone
        if (gone) {
          const g = (now - (V.goneAt || now)) / 1000;
          const amp = Math.min(60, 6 + g * 4);
          tx += (s.r - 0.5) * amp * 0.6;
          ty += Math.sin(t / 700 + i * 0.9) * amp + (s.r - 0.5) * amp;
        }
      }
      if (s.hold > now) {
        tx = s.hx;
        ty = s.hy;
      }
      s.vx += (tx - s.dx) * 0.22;
      s.vy += (ty - s.dy) * 0.22;
      s.vx *= 0.64;
      s.vy *= 0.64;
      s.dx += s.vx;
      s.dy += s.vy;
    }

    const source = altUntil > now && alt ? alt : tonedFor ? toned : img;
    const drawStrips = (c, ox) => {
      for (let i = 0; i < STRIPS; i++) {
        const s = strips[i];
        const sx = (i / STRIPS) * img.naturalWidth;
        const ssw = img.naturalWidth / STRIPS;
        c.drawImage(
          source,
          sx,
          0,
          ssw,
          img.naturalHeight,
          i * sw + s.dx + ox,
          s.dy,
          sw + 0.6,
          H,
        );
      }
    };
    ctx.clearRect(0, 0, W, H);
    drawStrips(ctx, 0);
    if (band && band.until > now) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, band.y * H, W, band.h * H);
      ctx.clip();
      ctx.clearRect(0, 0, W, H);
      drawStrips(ctx, band.dx);
      // multiply on the light sheet, screen on the dark one, so the colour
      // reads in both schemes
      ctx.globalCompositeOperation =
        tonedFor === "dark" ? "screen" : "multiply";
      ctx.globalAlpha = tonedFor === "dark" ? 0.7 : 1;
      ctx.fillStyle = band.col;
      ctx.fillRect(0, 0, W, H);
      ctx.restore();
    }
  }

  // Hard cuts on a random clock. Each one sets state the next draw honours.
  const glitches = {
    tear() {
      const a = Math.floor(rnd(0, STRIPS - 14));
      const n = Math.floor(rnd(4, 14));
      const dx = rnd(-70, 70);
      const dy = rnd(-30, 30);
      const until = Date.now() + rnd(120, 1800);
      for (let i = a; i < a + n; i++) {
        const s = strips[i];
        s.hx = dx;
        s.hy = dy;
        s.hold = until;
        s.dx = dx;
        s.dy = dy;
        s.vx = s.vy = 0;
      }
    },
    shuffle() {
      const until = Date.now() + rnd(200, 900);
      for (const s of strips) {
        if (Math.random() >= 0.3) continue;
        s.hx = rnd(-40, 40);
        s.hy = rnd(-120, 120);
        s.hold = until;
        s.dx = s.hx;
        s.dy = s.hy;
        s.vx = s.vy = 0;
      }
    },
    freeze() {
      freezeUntil = Date.now() + rnd(120, 700);
    },
    flash() {
      host.classList.add("neg");
      setTimeout(() => host.classList.remove("neg"), rnd(40, 120));
    },
    tint() {
      alt = reink(pick(Object.keys(INKS)));
      altUntil = Date.now() + rnd(80, 600);
    },
    band() {
      band = {
        y: rnd(0, 0.9),
        h: rnd(0.04, 0.3),
        dx: rnd(-50, 50),
        col: INKS[pick(Object.keys(INKS))][1],
        until: Date.now() + rnd(90, 700),
      };
    },
  };

  function tickGlitch() {
    if (!alive) return;
    const { idle, gone } = state();
    const { pool, mean } = glitchSchedule({
      idle,
      gone,
      hidden: document.hidden,
    });
    if (!reduceMotion) {
      try {
        glitches[pick(pool)]();
      } catch {
        // a glitch that cannot run this frame is skipped, never fatal
      }
    }
    glitchTimer = setTimeout(tickGlitch, nextGap(mean));
  }

  function frame(t) {
    if (!alive) return;
    if (Date.now() > freezeUntil) {
      try {
        draw(t);
      } catch {
        // drawing must never take the page down
      }
    }
    raf = requestAnimationFrame(frame);
  }

  const onMove = (e) => {
    const now = Date.now();
    const dt = Math.max(1, now - pt0);
    const inst = (Math.hypot(e.clientX - px0, e.clientY - py0) / dt) * 1000;
    V.speed = V.speed * 0.6 + inst * 0.4;
    px0 = e.clientX;
    py0 = e.clientY;
    pt0 = now;
    V.x = e.clientX;
    V.y = e.clientY;
    V.lastMove = now;
    V.gone = false;
    V.touch = e.pointerType === "touch";
  };
  // A finger is only a pointer while it is down: lifting it releases the
  // face, so a thumb scroll never leaves it shoved aside.
  const onRelease = (e) => {
    if (e.pointerType !== "touch") return;
    V.x = -9999;
    V.y = -9999;
    V.speed = 0;
    V.touch = true;
  };
  const onActivity = () => {
    V.lastMove = Date.now();
  };
  const onScroll = () => {
    const now = Date.now();
    V.scrollV =
      ((window.scrollY - V.lastScroll) / Math.max(1, now - V.lastScrollT)) *
      1000;
    V.lastScroll = window.scrollY;
    V.lastScrollT = now;
    V.lastMove = now;
  };
  const onLeave = () => {
    V.gone = true;
    V.goneAt = Date.now();
  };
  const onEnter = () => {
    V.gone = false;
  };
  const onDown = () => {
    V.lastMove = Date.now();
  };
  const onPortraitDown = () => {
    scatter = Math.max(scatter, 70);
    host.classList.add("neg");
    setTimeout(() => host.classList.remove("neg"), 110);
  };
  const onResize = () => size();

  window.addEventListener("pointermove", onMove, { passive: true });
  window.addEventListener("pointerup", onRelease, { passive: true });
  window.addEventListener("pointercancel", onRelease, { passive: true });
  window.addEventListener("pointerdown", onDown, { passive: true });
  window.addEventListener("keydown", onActivity, { passive: true });
  window.addEventListener("wheel", onActivity, { passive: true });
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onResize);
  document.documentElement.addEventListener("mouseleave", onLeave);
  document.documentElement.addEventListener("mouseenter", onEnter);
  host.addEventListener("pointerdown", onPortraitDown);

  img.onload = () => {
    size();
    tone();
  };
  img.src = src;
  raf = requestAnimationFrame(frame);
  glitchTimer = setTimeout(tickGlitch, nextGap(9000));

  return {
    destroy() {
      alive = false;
      cancelAnimationFrame(raf);
      clearTimeout(glitchTimer);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onRelease);
      window.removeEventListener("pointercancel", onRelease);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onActivity);
      window.removeEventListener("wheel", onActivity);
      window.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onResize);
      document.documentElement.removeEventListener("mouseleave", onLeave);
      document.documentElement.removeEventListener("mouseenter", onEnter);
      host.removeEventListener("pointerdown", onPortraitDown);
    },
  };
}
