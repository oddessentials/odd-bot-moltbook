/*
 * SiteFooter — Daily Dispatch design system
 *
 * One mascot moment per page lives here at the bottom: a small shrimp wave + colophon.
 * Keeps the design quiet but unmistakably ours.
 */

import { Link } from "wouter";
import { BRAND } from "@/lib/brand";

export function SiteFooter() {
  return (
    <footer className="mt-24 border-t border-border bg-secondary/40">
      <div className="container py-12">
        <div className="grid gap-10 md:grid-cols-[1fr_auto] md:items-end">
          <div className="flex items-start gap-4">
            <img
              src={BRAND.mascot.waving}
              alt=""
              className="h-14 w-14 shrink-0 object-contain"
            />
            <div>
              <p className="kicker-coral mb-2">Filed daily</p>
              <p
                className="font-display text-2xl font-semibold leading-tight text-foreground"
                style={{ fontVariationSettings: '"opsz" 144' }}
              >
                {BRAND.tagline}
              </p>
              <p className="mt-2 max-w-prose text-sm text-muted-foreground">
                A short, human-edited summary of what actually happened in the world of AI agents,
                published every weekday. Hosted by a small nerdy shrimp and a rotating cast of
                crustaceans.
              </p>
            </div>
          </div>

          <nav className="flex flex-wrap gap-x-6 gap-y-2 text-sm text-muted-foreground md:justify-end">
            <Link href="/" className="hover:text-foreground">Today</Link>
            <Link href="/archive" className="hover:text-foreground">Archive</Link>
            <Link href="/podcast" className="hover:text-foreground">Podcast</Link>
            <Link href="/about" className="hover:text-foreground">About</Link>
          </nav>
        </div>

        <div className="hairline mt-10" />

        <div className="mt-6 flex flex-col-reverse items-start justify-between gap-3 md:flex-row md:items-center">
          <p className="text-xs text-muted-foreground">
            © {new Date().getFullYear()} {BRAND.name}. All summaries are written for reading enjoyment, not investment advice.
          </p>
          <p className="text-xs text-muted-foreground">
            <span className="kicker">Made with</span>{" "}
            <span className="text-coral">♥</span>{" "}
            <span className="kicker">and a lot of kelp</span>
          </p>
        </div>
      </div>
    </footer>
  );
}

export default SiteFooter;
