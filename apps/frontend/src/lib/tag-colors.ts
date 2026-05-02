// Stable per-tag color assignment. Same tag name → same color across pages
// (inbox list rows, inbox header editor, broadcast modal chips, group-row
// chips). Distinct enough hues that 5–10 tags side-by-side don't visually
// blur into each other.
//
// We don't store the color on the tag row in the DB — it's derived from the
// name. That makes color stable without round-trips and means renaming or
// recreating a tag with the same name keeps its identity.

// Tailwind's JIT only compiles class strings it can statically see in source.
// That means we CAN'T do `bg-${color}-500/15` at runtime — those classes
// would never be generated. Hard-coded triples below.
const TAG_PALETTE: ReadonlyArray<{ idle: string; active: string; dot: string }> = [
  {
    idle: "bg-violet-500/15 text-violet-700 dark:text-violet-300 ring-violet-500/30",
    active: "bg-violet-600 text-white ring-violet-700/40",
    dot: "bg-violet-500",
  },
  {
    idle: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 ring-emerald-500/30",
    active: "bg-emerald-600 text-white ring-emerald-700/40",
    dot: "bg-emerald-500",
  },
  {
    idle: "bg-blue-500/15 text-blue-700 dark:text-blue-300 ring-blue-500/30",
    active: "bg-blue-600 text-white ring-blue-700/40",
    dot: "bg-blue-500",
  },
  {
    idle: "bg-amber-500/15 text-amber-800 dark:text-amber-300 ring-amber-500/30",
    active: "bg-amber-600 text-white ring-amber-700/40",
    dot: "bg-amber-500",
  },
  {
    idle: "bg-rose-500/15 text-rose-700 dark:text-rose-300 ring-rose-500/30",
    active: "bg-rose-600 text-white ring-rose-700/40",
    dot: "bg-rose-500",
  },
  {
    idle: "bg-cyan-500/15 text-cyan-700 dark:text-cyan-300 ring-cyan-500/30",
    active: "bg-cyan-600 text-white ring-cyan-700/40",
    dot: "bg-cyan-500",
  },
  {
    idle: "bg-fuchsia-500/15 text-fuchsia-700 dark:text-fuchsia-300 ring-fuchsia-500/30",
    active: "bg-fuchsia-600 text-white ring-fuchsia-700/40",
    dot: "bg-fuchsia-500",
  },
  {
    idle: "bg-lime-500/15 text-lime-800 dark:text-lime-300 ring-lime-500/30",
    active: "bg-lime-600 text-white ring-lime-700/40",
    dot: "bg-lime-500",
  },
  {
    idle: "bg-orange-500/15 text-orange-800 dark:text-orange-300 ring-orange-500/30",
    active: "bg-orange-600 text-white ring-orange-700/40",
    dot: "bg-orange-500",
  },
  {
    idle: "bg-sky-500/15 text-sky-700 dark:text-sky-300 ring-sky-500/30",
    active: "bg-sky-600 text-white ring-sky-700/40",
    dot: "bg-sky-500",
  },
  {
    idle: "bg-pink-500/15 text-pink-700 dark:text-pink-300 ring-pink-500/30",
    active: "bg-pink-600 text-white ring-pink-700/40",
    dot: "bg-pink-500",
  },
  {
    idle: "bg-indigo-500/15 text-indigo-700 dark:text-indigo-300 ring-indigo-500/30",
    active: "bg-indigo-600 text-white ring-indigo-700/40",
    dot: "bg-indigo-500",
  },
];

function hashName(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return h;
}

/** Returns the {idle, active, dot} class triple for a given tag name. */
export function tagColors(name: string): {
  idle: string;
  active: string;
  dot: string;
} {
  return TAG_PALETTE[hashName(name) % TAG_PALETTE.length];
}
