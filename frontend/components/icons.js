// Tiny inline SVG icon set — keeps the bundle lean and matches the Isomorphic
// stroked-line aesthetic.
import React from "react";

const I = ({ d, size = 18, fill = "none", strokeWidth = 1.8 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill={fill}
       stroke="currentColor" strokeWidth={strokeWidth}
       strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    {Array.isArray(d) ? d.map((p, i) => <path key={i} d={p} />) : <path d={d} />}
  </svg>
);

export const Stethoscope = (p) => (
  <I {...p} d={[
    "M6 3v6a4 4 0 0 0 8 0V3",
    "M10 19a4 4 0 0 0 8 0v-3",
    "M18 13a2 2 0 1 0 0 4 2 2 0 0 0 0-4z",
  ]} />
);
export const Mic = (p) => (
  <I {...p} d={[
    "M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z",
    "M5 10a7 7 0 0 0 14 0",
    "M12 17v4",
    "M8 21h8",
  ]} />
);
export const Stop = (p) => (
  <I {...p} fill="currentColor" strokeWidth={0}
     d="M7 7h10v10H7z" />
);
export const Upload = (p) => (
  <I {...p} d={[
    "M12 16V4",
    "M7 9l5-5 5 5",
    "M5 20h14",
  ]} />
);
export const Document = (p) => (
  <I {...p} d={[
    "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z",
    "M14 3v5h5",
    "M9 13h6",
    "M9 17h6",
  ]} />
);
export const Brain = (p) => (
  <I {...p} d={[
    "M9 4a3 3 0 0 0-3 3v1a3 3 0 0 0-2 3 3 3 0 0 0 2 3 3 3 0 0 0 3 3 3 3 0 0 0 3-3V4z",
    "M15 4a3 3 0 0 1 3 3v1a3 3 0 0 1 2 3 3 3 0 0 1-2 3 3 3 0 0 1-3 3 3 3 0 0 1-3-3V4z",
  ]} />
);
export const Sparkle = (p) => (
  <I {...p} d={[
    "M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z",
  ]} />
);
export const Copy = (p) => (
  <I {...p} d={[
    "M9 9h10v10H9z",
    "M5 15V5h10",
  ]} />
);
export const Download = (p) => (
  <I {...p} d={[
    "M12 4v12",
    "M7 11l5 5 5-5",
    "M5 20h14",
  ]} />
);
export const Activity = (p) => (
  <I {...p} d="M3 12h4l3-8 4 16 3-8h4" />
);
export const Settings = (p) => (
  <I {...p} d={[
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
    "M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  ]} />
);
export const Folder = (p) => (
  <I {...p} d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
);
export const Heart = (p) => (
  <I {...p} d="M20.8 6.6a5.4 5.4 0 0 0-7.7 0L12 7.7l-1.1-1.1a5.4 5.4 0 0 0-7.7 7.7l1.1 1.1L12 23l7.7-7.7 1.1-1.1a5.4 5.4 0 0 0 0-7.7z" />
);
export const Image = (p) => (
  <I {...p} d={[
    "M3 5h18v14H3z",
    "M3 16l5-5 4 4 3-3 6 6",
    "M8 9a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z",
  ]} />
);
export const Send = (p) => (
  <I {...p} d={[
    "M22 2L11 13",
    "M22 2l-7 20-4-9-9-4z",
  ]} />
);
export const Trash = (p) => (
  <I {...p} d={[
    "M3 6h18",
    "M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
    "M6 6l1 14a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-14",
  ]} />
);
export const XRay = (p) => (
  <I {...p} d={[
    "M4 4h16v16H4z",
    "M4 12h16",
    "M12 4v16",
    "M8 8l8 8",
    "M16 8l-8 8",
  ]} />
);
export const Pill = (p) => (
  <I {...p} d={[
    "M9 4l-5 5a4.95 4.95 0 0 0 7 7l5-5a4.95 4.95 0 0 0-7-7z",
    "M8 13l5-5",
  ]} />
);
export const Bell = (p) => (
  <I {...p} d={[
    "M6 8a6 6 0 1 1 12 0c0 7 3 7 3 9H3c0-2 3-2 3-9z",
    "M10 21a2 2 0 0 0 4 0",
  ]} />
);
export const GradCap = (p) => (
  <I {...p} d={[
    "M2 9l10-5 10 5-10 5L2 9z",
    "M6 11v5a6 6 0 0 0 12 0v-5",
  ]} />
);
export const Globe = (p) => (
  <I {...p} d={[
    "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20z",
    "M2 12h20",
    "M12 2a14 14 0 0 1 0 20",
    "M12 2a14 14 0 0 0 0 20",
  ]} />
);
export const User = (p) => (
  <I {...p} d={[
    "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    "M4 22a8 8 0 0 1 16 0",
  ]} />
);
export const LogOut = (p) => (
  <I {...p} d={[
    "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4",
    "M16 17l5-5-5-5",
    "M21 12H9",
  ]} />
);
