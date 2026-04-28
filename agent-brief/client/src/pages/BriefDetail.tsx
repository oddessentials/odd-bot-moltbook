/*
 * BriefDetail — Daily Dispatch design system
 *
 * Single-column reading experience for one day's brief.
 * - Top: kicker (issue + date), oversized headline, dek, tag chips
 * - Body: numbered items with mono numerals + Fraunces sub-headline + Inter body
 * - Reading-progress hairline at top of page
 * - "Up next" footer with prev/next chronological links
 */

import { useEffect, useState } from "react";
import { Link, useParams } from "wouter";
import { ArrowLeft, ArrowRight, Calendar } from "lucide-react";
import SiteLayout from "@/components/SiteLayout";
import { Button } from "@/components/ui/button";
import { formatLongDate } from "@/data/content";
import { useBriefs } from "@/hooks/useBriefs";
import NotFound from "./NotFound";

function useReadingProgress() {
  const [progress, setProgress] = useState(0);
  useEffect(() => {
    const onScroll = () => {
      const h = document.documentElement;
      const top = h.scrollTop;
      const max = h.scrollHeight - h.clientHeight;
      setProgress(max > 0 ? Math.min(1, Math.max(0, top / max)) : 0);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  return progress;
}

export default function BriefDetail() {
  const params = useParams<{ id: string }>();
  const id = params.id ?? "";
  const { briefs, loading } = useBriefs();
  const brief = briefs.find((b) => b.id === id);
  const progress = useReadingProgress();

  // While the briefs fetch is in flight we don't yet know whether the
  // requested id exists. Render NotFound only once we have the list.
  if (!brief) return loading ? <SiteLayout><div className="container py-20" /></SiteLayout> : <NotFound />;

  // Briefs are ordered newest-first; chronological "newer" is the entry before, "older" is after.
  const idx = briefs.findIndex((b) => b.id === brief.id);
  const newer = idx > 0 ? briefs[idx - 1] : null;
  const older = idx < briefs.length - 1 ? briefs[idx + 1] : null;

  return (
    <SiteLayout>
      <div
        className="reading-progress"
        style={{ transform: `scaleX(${progress})` }}
        aria-hidden
      />

      <article className="container max-w-3xl pt-10 pb-20 md:pt-14">
        <Link
          href="/archive"
          className="inline-flex items-center gap-1 text-sm font-medium text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" /> Back to archive
        </Link>

        <header className="mt-8">
          <div className="flex flex-wrap items-center gap-3">
            <span className="kicker-coral">
              Issue No. {String(brief.issueNo).padStart(3, "0")}
            </span>
            <span className="kicker">{formatLongDate(brief.date)}</span>
            <span className="kicker">{brief.readingMinutes} min read</span>
          </div>

          <h1
            className="display-headline mt-5 text-4xl text-foreground md:text-5xl"
            style={{ fontVariationSettings: '"opsz" 144' }}
          >
            {brief.title}
          </h1>

          <p className="prose-body mt-5 text-lg text-foreground/85">{brief.dek}</p>

          <div className="mt-6 flex flex-wrap gap-2">
            {brief.tags.map((t) => (
              <span
                key={t}
                className="rounded-full border border-border px-2.5 py-0.5 text-[11px] font-medium text-foreground/70"
              >
                {t}
              </span>
            ))}
          </div>
        </header>

        <div className="hairline mt-10" />

        <div className="mt-10 space-y-12">
          {brief.items.map((item, idx) => (
            <section key={item.headline} className="grid gap-3 sm:grid-cols-[3rem_1fr]">
              <div className="font-mono text-2xl font-semibold leading-none text-coral">
                {String(idx + 1).padStart(2, "0")}
              </div>
              <div>
                <h2
                  className="font-display text-2xl font-semibold leading-tight tracking-tight"
                  style={{ fontVariationSettings: '"opsz" 96' }}
                >
                  {item.headline}
                </h2>
                <p className="mt-3 leading-relaxed text-foreground/85">{item.body}</p>
                {item.source && (
                  <p className="kicker mt-2">Source · {item.source}</p>
                )}
              </div>
            </section>
          ))}
        </div>

        <div className="hairline mt-16" />

        <p className="mt-6 text-xs italic leading-relaxed text-muted-foreground">
          {brief.disclaimer}
        </p>

        <nav className="mt-8 grid gap-4 sm:grid-cols-2">
          {older ? (
            <Link
              href={`/brief/${older.id}`}
              className="group flex h-full flex-col rounded-lg border border-border p-5 transition-colors hover:border-foreground/20 hover:bg-muted/40"
            >
              <span className="kicker">Older brief</span>
              <span
                className="mt-2 font-display text-lg font-semibold leading-tight"
                style={{ fontVariationSettings: '"opsz" 96' }}
              >
                {older.title}
              </span>
              <span className="mt-auto pt-3 inline-flex items-center gap-1 text-sm text-foreground/60 group-hover:text-foreground">
                <ArrowLeft className="h-4 w-4" /> {formatLongDate(older.date)}
              </span>
            </Link>
          ) : (
            <div className="rounded-lg border border-dashed border-border p-5 text-sm text-muted-foreground">
              <span className="kicker">Older brief</span>
              <p className="mt-2">This is our earliest published brief.</p>
            </div>
          )}

          {newer ? (
            <Link
              href={`/brief/${newer.id}`}
              className="group flex h-full flex-col rounded-lg border border-border p-5 text-right transition-colors hover:border-foreground/20 hover:bg-muted/40"
            >
              <span className="kicker self-end">Newer brief</span>
              <span
                className="mt-2 font-display text-lg font-semibold leading-tight"
                style={{ fontVariationSettings: '"opsz" 96' }}
              >
                {newer.title}
              </span>
              <span className="mt-auto pt-3 inline-flex items-center justify-end gap-1 text-sm text-foreground/60 group-hover:text-foreground">
                {formatLongDate(newer.date)} <ArrowRight className="h-4 w-4" />
              </span>
            </Link>
          ) : (
            <Link
              href="/"
              className="group flex h-full flex-col rounded-lg border border-coral/30 bg-coral/5 p-5 text-right transition-colors hover:border-coral/50"
            >
              <span className="kicker-coral self-end">You&rsquo;re caught up</span>
              <span
                className="mt-2 font-display text-lg font-semibold leading-tight text-foreground"
                style={{ fontVariationSettings: '"opsz" 96' }}
              >
                Back to today&rsquo;s brief
              </span>
              <span className="mt-auto pt-3 inline-flex items-center justify-end gap-1 text-sm text-foreground/70 group-hover:text-foreground">
                <Calendar className="h-4 w-4" /> Today
              </span>
            </Link>
          )}
        </nav>

        <div className="mt-12 flex items-center justify-center">
          <Link href="/archive">
            <Button variant="outline" className="rounded-full">
              Browse the full archive <ArrowRight className="ml-1 h-4 w-4" />
            </Button>
          </Link>
        </div>
      </article>
    </SiteLayout>
  );
}
