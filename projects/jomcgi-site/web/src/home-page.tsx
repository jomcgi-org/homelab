import { useEffect, useRef, useState } from "react";
import { HOME_KEYS, HOME_POSTS, sectionHref } from "./home-data";
import { HomeScheme, scheme } from "./home-scheme";
import portraitSrc from "./assets/joe-grey.png";
import "./home.css";

type Reading = { big: string; small: string; primary?: boolean };

export function HomePage() {
  const [reading, setReading] = useState<Reading | null>(null);
  const [slopOn, setSlopOn] = useState(false);
  const [clock, setClock] = useState("--:--:--");
  const [ledOff, setLedOff] = useState(false);
  const [cursorOff, setCursorOff] = useState(false);
  const pad = useRef<HTMLElement>(null);
  const frame = useRef<HTMLDivElement>(null);
  const print = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const slopTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  function later(fn: () => void, ms: number) {
    timers.current.push(setTimeout(fn, ms));
  }

  function readout(item: Reading | null, pointerType?: string) {
    if (pointerType === "touch") return;
    if (item && window.matchMedia("(max-width: 760px)").matches) return;
    clearTimeout(slopTimer.current);
    setReading(item);
    setSlopOn(false);
    if (item?.primary)
      slopTimer.current = setTimeout(() => setSlopOn(true), 60);
  }

  function press(index: number) {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const caps = [...(pad.current?.querySelectorAll(".key") ?? [])];
    caps.forEach((key, j) => {
      if (j === index) return;
      const distance = Math.hypot(
        (j % 3) - (index % 3),
        Math.floor(j / 3) - Math.floor(index / 3),
      );
      later(() => {
        key.classList.add("nudge");
        later(() => key.classList.remove("nudge"), 90);
      }, distance * 45);
    });
  }

  useEffect(() => {
    let alive = true;
    let started = false;
    let portrait: { destroy(): void } | undefined;
    const reduce = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    // Keep the pre-rendered image without JS. Load the canvas engine on intent.
    const startPortrait = () => {
      if (started || reduce) return;
      started = true;
      void import("./home-portrait.js")
        .then(({ createPortrait }) => {
          if (!alive || !canvas.current || !print.current || !frame.current)
            return;
          portrait = createPortrait({
            canvas: canvas.current,
            host: print.current,
            tapTarget: frame.current,
            src: portraitSrc,
            scheme,
            reduceMotion: reduce,
          });
        })
        .catch(() => {
          started = false;
        });
    };
    const intents = ["pointermove", "pointerdown", "keydown"] as const;
    for (const event of intents)
      window.addEventListener(event, startPortrait, { passive: true });

    const tz = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/London",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
    setClock(tz.format(new Date()));
    const clockTimer = setInterval(() => setClock(tz.format(new Date())), 1000);
    const blink = () => {
      setLedOff(true);
      later(() => setLedOff(false), 90 + Math.random() * 120);
      later(blink, 700 + Math.random() * 3800);
    };
    let cursor = false;
    const cblink = () => {
      cursor = !cursor;
      setCursorOff(cursor);
      later(
        cblink,
        cursor ? 180 + Math.random() * 220 : 400 + Math.random() * 1400,
      );
    };
    if (!reduce) {
      blink();
      cblink();
    }
    return () => {
      alive = false;
      portrait?.destroy();
      for (const event of intents)
        window.removeEventListener(event, startPortrait);
      clearInterval(clockTimer);
      clearTimeout(slopTimer.current);
      for (const timer of timers.current) clearTimeout(timer);
      timers.current = [];
    };
  }, []);

  const cursor = (
    <span className={`cursor${cursorOff ? " off" : ""}`} aria-hidden="true" />
  );
  return (
    <main className="home">
      <div className="unit">
        <div className="plate">
          <span className="brand">jomcgi.dev</span>
          <span className="meta">
            <span>
              scotland <b>{clock}</b>
            </span>
            <span className="led">
              <i className={ledOff ? "off" : undefined} aria-hidden="true" />{" "}
              <span>live</span>
            </span>
          </span>
          <HomeScheme />
        </div>
        <div className={`glass${slopOn ? " slop" : ""}`} aria-live="polite">
          <h1 className={`big${reading ? " sr-only" : ""}`}>
            Joe McGinley, Scotland.{!reading && cursor}
          </h1>
          {reading ? (
            <>
              <p className="big" aria-hidden="true">
                {reading.primary ? (
                  <>
                    <span className="sw">Software</span>{" "}
                    <span className="sl">Slop&nbsp;</span>factory
                  </>
                ) : (
                  reading.big
                )}
                {cursor}
              </p>
              <p className="small">{reading.small}</p>
            </>
          ) : (
            <p className="small">
              Building production grade infra for weekend side quests and
              keeping <a href="https://semgrep.dev">Semgrep</a> online.
            </p>
          )}
        </div>
        <div className="deck">
          <div
            className="portrait"
            ref={frame}
            role="img"
            aria-label="Joe, a greyscale print behind a perforated grille, cut into strips that scatter when you move"
          >
            <div className="print" ref={print}>
              <img className="portrait-still" src={portraitSrc} alt="" />
              <canvas ref={canvas} aria-hidden="true" />
            </div>
            <div className="holes" />
          </div>
          <div className="right">
            <nav
              className="pad"
              aria-label="Site"
              ref={pad}
              onPointerLeave={() => readout(null)}
            >
              {HOME_KEYS.map((key, i) => (
                <a
                  key={key.section}
                  className={`key${key.primary ? " go" : ""}`}
                  href={sectionHref(key.section)}
                  onPointerEnter={(event) => readout(key, event.pointerType)}
                  onPointerDown={() => press(i)}
                  onFocus={() => readout(key)}
                  onBlur={() => readout(null)}
                >
                  <span className="cap">
                    <i />
                  </span>
                  <span className="legend">{key.legend}</span>
                </a>
              ))}
            </nav>
            {HOME_POSTS.length > 0 && (
              <section
                className="writing"
                aria-label="Writing"
                onPointerLeave={() => readout(null)}
              >
                <span className="lab">writing</span>
                {HOME_POSTS.map((post) => (
                  <a
                    key={post.slug}
                    href={`${sectionHref("Blog")}/${post.slug}`}
                    onPointerEnter={(event) =>
                      readout(
                        { big: post.title, small: post.summary },
                        event.pointerType,
                      )
                    }
                    onFocus={() =>
                      readout({ big: post.title, small: post.summary })
                    }
                    onBlur={() => readout(null)}
                  >
                    <time dateTime={post.date}>{post.date}</time>
                    <span>{post.title}</span>
                  </a>
                ))}
              </section>
            )}
          </div>
        </div>
        <div className="foot">
          <span className="links">
            <a href="mailto:joe@jomcgi.dev">joe@jomcgi.dev</a>
            <a
              href="https://github.com/jomcgi"
              target="_blank"
              rel="noreferrer"
            >
              github
            </a>
            <a
              href="https://linkedin.com/in/jomcgi"
              target="_blank"
              rel="noreferrer"
            >
              linkedin
            </a>
          </span>
          <nav aria-label="Continue reading">
            {[
              "CV",
              "Engineering",
              ...(HOME_POSTS.length ? ["Blog"] : []),
              "Docs",
            ].map((name) => (
              <a key={name} href={sectionHref(name)}>
                {name.toLowerCase()}
              </a>
            ))}
          </nav>
        </div>
      </div>
    </main>
  );
}
