/*
 * Brand assets — single source of truth.
 *
 * The Agent Brief uses one signature mascot (a small nerdy shrimp) shown sparingly:
 *   - waving in the logo lockup and footer
 *   - reading on the About page
 *   - with headphones on the Podcast page
 *   - waving on the 404 page
 *
 * Images are CDN-hosted and tied to the project's lifecycle.
 */

export const BRAND = {
  name: "The Agent Brief",
  short: "Agent Brief",
  tagline: "A short daily on AI agents.",
  mascot: {
    waving:
      "https://d2xsxph8kpxj0f.cloudfront.net/310519663371880427/LtFdh4mqUcJcxBN7uBeCFV/shrimp-mascot-54sdiZQJXeJrCF8bUBijtR.webp",
    reading:
      "https://d2xsxph8kpxj0f.cloudfront.net/310519663371880427/LtFdh4mqUcJcxBN7uBeCFV/shrimp-mascot-reading-527zvUdqxSYHV3PamDLmZ3.webp",
    podcast:
      "https://d2xsxph8kpxj0f.cloudfront.net/310519663371880427/LtFdh4mqUcJcxBN7uBeCFV/shrimp-mascot-podcast-MiSoQFWdQoipkgawzPRQcc.webp",
  },
  heroTexture:
    "https://d2xsxph8kpxj0f.cloudfront.net/310519663371880427/LtFdh4mqUcJcxBN7uBeCFV/hero-texture-hz92ikJY7CWzhCvmnrJ33D.webp",
} as const;
