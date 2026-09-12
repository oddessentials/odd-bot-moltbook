/*
 * Brand assets — single source of truth.
 *
 * The Agent Brief uses one signature mascot (a small nerdy shrimp) shown sparingly:
 *   - waving in the logo lockup and footer
 *   - reading on the About page
 *   - with headphones on the Podcast page
 *   - waving on the 404 page
 *
 * These are REPO assets, served from our own origin. They live in
 * `client/public/brand/` and Vite copies them into `docs/` on every build,
 * exactly like the favicon set.
 *
 * They used to be hotlinked from the Manus scaffolder's CloudFront bucket.
 * That bucket started returning 403 in Sept 2026 and every mascot on the site
 * went blank at once — header and footer on every page, About, Podcast, 404,
 * plus the hero wash. Nothing in the repo had to change for the site to break,
 * which is the whole argument for not hotlinking brand art. Do not point these
 * at a third-party origin again.
 */

export const BRAND = {
  name: "The Agent Brief",
  short: "Agent Brief",
  tagline: "A short daily on AI agents.",
  mascot: {
    waving: "/brand/shrimp-waving.webp",
    reading: "/brand/shrimp-reading.webp",
    podcast: "/brand/shrimp-podcast.webp",
  },
  heroTexture: "/brand/hero-texture.webp",
} as const;
