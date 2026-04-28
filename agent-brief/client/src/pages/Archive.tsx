/*
 * Archive — Daily Dispatch design system
 *
 * A clean, scannable index of past briefs.
 *   - Top: small intro + search input + tag filter row
 *   - Body: two-column "date · content" rows grouped by month
 *
 * Filtering is purely client-side over the briefs array; the pipeline can
 * extend the data without changing this view.
 */

import { useMemo, useState } from "react";
import { Link } from "wouter";
import { Search, X } from "lucide-react";
import SiteLayout from "@/components/SiteLayout";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { allTags, formatLongDate } from "@/data/content";
import type { Brief, BriefTag } from "@/data/content";
import { useBriefs } from "@/hooks/useBriefs";

function monthKey(iso: string): string {
  const d = new Date(iso + (iso.length === 10 ? "T12:00:00Z" : ""));
  return d.toLocaleDateString("en-US", { month: "long", year: "numeric" });
}

export default function Archive() {
  const { briefs } = useBriefs();
  const [query, setQuery] = useState("");
  const [activeTags, setActiveTags] = useState<Set<BriefTag>>(new Set());

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return briefs.filter((b) => {
      if (activeTags.size > 0 && !Array.from(activeTags).every((t) => b.tags.includes(t))) {
        // require ALL selected tags to be present (AND filter)
        return false;
      }
      if (!q) return true;
      const hay = (b.title + " " + b.dek + " " + b.tags.join(" ")).toLowerCase();
      return hay.includes(q);
    });
  }, [briefs, query, activeTags]);

  const grouped = useMemo(() => {
    const map = new Map<string, Brief[]>();
    filtered.forEach((b) => {
      const key = monthKey(b.date);
      const arr = map.get(key) ?? [];
      arr.push(b);
      map.set(key, arr);
    });
    return Array.from(map.entries());
  }, [filtered]);

  function toggleTag(t: BriefTag) {
    setActiveTags((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  }

  return (
    <SiteLayout>
      <header className="border-b border-border bg-secondary/30">
        <div className="container py-12 md:py-16">
          <p className="kicker-coral">Archive</p>
          <h1
            className="display-headline mt-3 text-4xl md:text-5xl"
            style={{ fontVariationSettings: '"opsz" 144' }}
          >
            Every brief, all the way back.
          </h1>
          <p className="mt-3 max-w-2xl text-base text-muted-foreground">
            A short, dated record of what happened in AI agents. Search by keyword or filter by tag.
          </p>

          <div className="mt-8 grid gap-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
            <div className="relative">
              <Search className="pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search briefs by keyword…"
                className="h-12 rounded-full bg-background pl-11 pr-10 text-base"
              />
              {query && (
                <button
                  onClick={() => setQuery("")}
                  className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                  aria-label="Clear search"
                >
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              {allTags.map((t) => {
                const on = activeTags.has(t);
                return (
                  <button
                    key={t}
                    onClick={() => toggleTag(t)}
                    className={
                      "rounded-full border px-3 py-1.5 text-xs font-medium transition-colors " +
                      (on
                        ? "border-coral bg-coral text-white"
                        : "border-border text-foreground/70 hover:border-foreground/30 hover:text-foreground")
                    }
                  >
                    {t}
                  </button>
                );
              })}
              {(activeTags.size > 0 || query) && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setActiveTags(new Set());
                    setQuery("");
                  }}
                  className="rounded-full text-xs"
                >
                  Clear
                </Button>
              )}
            </div>
          </div>
        </div>
      </header>

      <section className="container py-14">
        {grouped.length === 0 ? (
          <div className="mx-auto max-w-md py-20 text-center">
            <p className="kicker-coral">No matches</p>
            <h2
              className="mt-3 font-display text-2xl font-semibold"
              style={{ fontVariationSettings: '"opsz" 96' }}
            >
              We couldn&rsquo;t find a brief that fits.
            </h2>
            <p className="mt-2 text-sm text-muted-foreground">
              Try different keywords or clear the filters.
            </p>
            <Button
              variant="outline"
              className="mt-5 rounded-full"
              onClick={() => {
                setQuery("");
                setActiveTags(new Set());
              }}
            >
              Reset filters
            </Button>
          </div>
        ) : (
          <div className="space-y-14">
            {grouped.map(([month, items]) => (
              <div key={month}>
                <div className="mb-6 flex items-baseline gap-3">
                  <h2
                    className="font-display text-2xl font-semibold tracking-tight"
                    style={{ fontVariationSettings: '"opsz" 144' }}
                  >
                    {month}
                  </h2>
                  <span className="kicker">{items.length} brief{items.length === 1 ? "" : "s"}</span>
                </div>

                <ul className="divide-y divide-border rounded-lg border border-border bg-card">
                  {items.map((b) => (
                    <li key={b.id}>
                      <Link
                        href={`/brief/${b.id}`}
                        className="group grid items-baseline gap-3 px-5 py-5 transition-colors hover:bg-muted/40 sm:grid-cols-[10.5rem_minmax(0,1fr)_auto] sm:gap-6"
                      >
                        <div className="kicker text-foreground/70">
                          {formatLongDate(b.date)}
                        </div>
                        <div>
                          <h3
                            className="font-display text-lg font-semibold leading-snug tracking-tight text-card-foreground"
                            style={{ fontVariationSettings: '"opsz" 96' }}
                          >
                            <span className="bg-[length:0%_2px] bg-gradient-to-r from-coral to-coral bg-no-repeat bg-bottom pb-0.5 transition-[background-size] duration-300 group-hover:bg-[length:100%_2px]">
                              {b.title}
                            </span>
                          </h3>
                          <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{b.dek}</p>
                        </div>
                        <div className="flex flex-wrap gap-1.5 sm:justify-end">
                          {b.tags.slice(0, 2).map((t) => (
                            <span
                              key={t}
                              className="rounded-full border border-border px-2 py-0.5 text-[11px] font-medium text-foreground/65"
                            >
                              {t}
                            </span>
                          ))}
                        </div>
                      </Link>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )}
      </section>
    </SiteLayout>
  );
}
