/*
 * BriefCard — Daily Dispatch design system
 *
 * The archetypal "daily brief" card used on Home and Archive.
 * - kicker line: ISSUE NO. + date in mono
 * - Fraunces headline
 * - 1–2 sentence Inter dek
 * - tag chips at the bottom
 * - subtle lift on hover, coral underline on the headline
 */

import { Link } from "wouter";
import { ArrowRight } from "lucide-react";
import type { Brief } from "@/data/content";
import { formatShortDate } from "@/data/content";

interface BriefCardProps {
  brief: Brief;
  highlightToday?: boolean;
}

export function BriefCard({ brief, highlightToday }: BriefCardProps) {
  return (
    <Link
      href={`/brief/${brief.id}`}
      className="group relative flex h-full flex-col rounded-lg border border-border bg-card p-6 transition-all duration-200 hover:-translate-y-0.5 hover:border-foreground/20 hover:shadow-[0_8px_30px_-12px_rgba(0,0,0,0.12)]"
    >
      <div className="flex items-center justify-between">
        <span className="kicker">
          Issue No. {String(brief.issueNo).padStart(3, "0")}
        </span>
        <span className="kicker">{formatShortDate(brief.date)}</span>
      </div>

      <h3
        className="mt-4 font-display text-[1.35rem] font-semibold leading-[1.15] tracking-tight text-card-foreground"
        style={{ fontVariationSettings: '"opsz" 96' }}
      >
        <span className="bg-[length:0%_2px] bg-gradient-to-r from-coral to-coral bg-no-repeat bg-bottom pb-0.5 transition-[background-size] duration-300 group-hover:bg-[length:100%_2px]">
          {brief.title}
        </span>
      </h3>

      <p className="mt-3 line-clamp-3 text-sm leading-relaxed text-muted-foreground">
        {brief.dek}
      </p>

      <div className="mt-auto pt-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap gap-1.5">
            {brief.tags.slice(0, 3).map((t) => (
              <span
                key={t}
                className="rounded-full border border-border px-2 py-0.5 text-[11px] font-medium text-foreground/70"
              >
                {t}
              </span>
            ))}
          </div>
          <span className="inline-flex items-center gap-1 text-xs font-medium text-foreground/60 transition-colors group-hover:text-foreground">
            {highlightToday ? "Read today's brief" : `${brief.readingMinutes} min read`}
            <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
          </span>
        </div>
      </div>

      {highlightToday && (
        <span
          className="absolute -top-1 -right-1 inline-flex items-center gap-1.5 rounded-full bg-coral px-2.5 py-0.5 text-[10px] font-mono font-semibold uppercase tracking-wider text-white shadow-md"
          aria-label="Today"
        >
          Today
        </span>
      )}
    </Link>
  );
}

export default BriefCard;
