import { useEffect, useState } from "react";

/**
 * Live, medical/radiology-themed animated backdrop.
 *
 * Three stacked layers:
 *   1. A cross-fading carousel of high-quality radiology / MRI / CT photos
 *      streamed from Unsplash's CDN (no key required).
 *   2. An animated SVG overlay: a sweeping CT-style scan line, an ECG trace,
 *      and a subtle hex/grid pattern.
 *   3. A frosted gradient veil so foreground UI remains readable.
 */
const IMAGES = [
  // curated radiology / clinical imagery (Unsplash, ORB-friendly jpegs)
  "https://images.unsplash.com/photo-1530026405186-ed1f139313f8?auto=format&fit=crop&w=2000&q=85&fm=jpg", // MRI bore
  "https://images.unsplash.com/photo-1576091160550-2173dba999ef?auto=format&fit=crop&w=2000&q=85&fm=jpg", // doctor at monitor
  "https://images.unsplash.com/photo-1631815588090-d4bfec5b1ccb?auto=format&fit=crop&w=2000&q=85&fm=jpg", // CT scanner
  "https://images.unsplash.com/photo-1581595219315-a187dd40c322?auto=format&fit=crop&w=2000&q=85&fm=jpg", // reading room
  "https://images.unsplash.com/photo-1538108149393-fbbd81895907?auto=format&fit=crop&w=2000&q=85&fm=jpg", // doctor / stethoscope
  "https://images.unsplash.com/photo-1505751172876-fa1923c5c528?auto=format&fit=crop&w=2000&q=85&fm=jpg", // medical team
  "https://images.unsplash.com/photo-1516549655169-df83a0774514?auto=format&fit=crop&w=2000&q=85&fm=jpg", // x-ray hand
  "https://images.unsplash.com/photo-1519494026892-80bbd2d6fd0d?auto=format&fit=crop&w=2000&q=85&fm=jpg", // clinician with tablet
];

export default function BackgroundFx() {
  const [idx, setIdx] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setIdx((i) => (i + 1) % IMAGES.length), 9000);
    return () => clearInterval(t);
  }, []);

  return (
    <div className="bgfx" aria-hidden="true">
      {/* layer 1 — image carousel (cross-fade) */}
      <div className="bgfx-photos">
        {IMAGES.map((src, i) => (
          <div
            key={src}
            className={`bgfx-photo ${i === idx ? "on" : ""}`}
            style={{ backgroundImage: `url(${src})` }}
          />
        ))}
      </div>

      {/* layer 2 — animated SVG overlay */}
      <svg className="bgfx-svg" viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid slice">
        <defs>
          <linearGradient id="scan" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%"   stopColor="#0ea5a4" stopOpacity="0" />
            <stop offset="50%"  stopColor="#0ea5a4" stopOpacity=".55" />
            <stop offset="100%" stopColor="#0ea5a4" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="ecg" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0%"   stopColor="#2563eb" stopOpacity="0" />
            <stop offset="50%"  stopColor="#2563eb" stopOpacity=".7" />
            <stop offset="100%" stopColor="#2563eb" stopOpacity="0" />
          </linearGradient>
          <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#ffffff" strokeOpacity=".05" strokeWidth="1"/>
          </pattern>
        </defs>

        {/* hex/grid wash */}
        <rect width="1600" height="900" fill="url(#grid)" />

        {/* sweeping CT-style scan beam */}
        <g className="bgfx-scan">
          <rect x="-300" y="0" width="300" height="900" fill="url(#scan)" />
        </g>

        {/* ECG pulse trace */}
        <g className="bgfx-ecg">
          <path
            d="M0 720 L300 720 L320 720 L340 700 L360 760 L380 620 L400 820 L420 720 L460 720 L1600 720"
            fill="none" stroke="url(#ecg)" strokeWidth="2.4"
          />
        </g>

        {/* rotating ring like a CT gantry */}
        <g className="bgfx-ring" transform="translate(1280 720)">
          <circle r="140" fill="none" stroke="#0ea5a4" strokeOpacity=".25" strokeWidth="1.2" />
          <circle r="140" fill="none" stroke="#0ea5a4" strokeOpacity=".7"  strokeWidth="2"
                  strokeDasharray="80 800" />
          <circle r="100" fill="none" stroke="#2563eb" strokeOpacity=".2"  strokeWidth="1" />
          <circle r="60"  fill="none" stroke="#ffffff" strokeOpacity=".18" strokeWidth="1" />
        </g>

        {/* drifting particles */}
        <g className="bgfx-particles">
          {Array.from({ length: 22 }).map((_, i) => (
            <circle key={i}
              cx={(i * 73) % 1600}
              cy={(i * 137) % 900}
              r={1 + (i % 3)}
              fill="#ffffff" fillOpacity={0.18}
              style={{ animationDelay: `${(i % 7) * 0.6}s` }}
            />
          ))}
        </g>
      </svg>

      {/* layer 3 — frosted readability veil */}
      <div className="bgfx-veil" />
    </div>
  );
}
