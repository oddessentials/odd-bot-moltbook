/*
 * Home — Daily Dispatch design system
 *
 * Sections:
 *   1. "Today's Brief" hero card (oversized headline, dek, items preview, CTA)
 *   2. Recent briefs (3-up card grid)
 *   3. Latest episode (inline YouTube embed + description)
 *   4. Follow-on-X strip (CTA card pointing to @oddessentials)
 *
 * One mascot moment: small waving shrimp tucked into the hero corner on desktop.
 */

import { Link } from "wouter";
import { ArrowRight, Calendar } from "lucide-react";
import { Button } from "@/components/ui/button";

import SiteLayout from "@/components/SiteLayout";
import BriefCard from "@/components/BriefCard";
import { formatLongDate, type Brief } from "@/data/content";
import { useBriefs } from "@/hooks/useBriefs";
import { useEpisodes } from "@/hooks/useEpisodes";
import { BRAND } from "@/lib/brand";

function HeroBrief({ briefs }: { briefs: Brief[] }) {
  const today = briefs[0];
  if (!today) return null;
  return (
    <section className="relative overflow-hidden border-b border-border">
      {/* Soft texture wash */}
      <div
        className="absolute inset-0 -z-10 opacity-70"
        style={{
          backgroundImage: `url(${BRAND.heroTexture})`,
          backgroundSize: "cover",
          backgroundPosition: "center",
        }}
        aria-hidden
      />
      <div className="absolute inset-0 -z-10 bg-gradient-to-b from-background/40 via-background/85 to-background" aria-hidden />
      <div className="grain absolute inset-0 -z-10" aria-hidden />

      <div className="container relative pt-12 pb-16 md:pt-20 md:pb-24">
        <div className="grid gap-10 lg:grid-cols-[1.35fr_1fr] lg:items-end">
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <span className="inline-flex items-center gap-2 rounded-full border border-coral/30 bg-coral/10 px-3 py-1 text-[11px] font-mono font-semibold uppercase tracking-wider text-coral">
                <span className="coral-dot" /> Today
              </span>
              <span className="kicker">
                Issue No. {String(today.issueNo).padStart(3, "0")}
              </span>
              <span className="kicker">{formatLongDate(today.date)}</span>
            </div>

            <h1
              className="display-headline mt-6 text-4xl text-foreground sm:text-5xl md:text-6xl"
              style={{ fontVariationSettings: '"opsz" 144' }}
            >
              {today.title}
            </h1>

            <p className="mt-5 max-w-2xl text-lg leading-relaxed text-foreground/80">
              {today.dek}
            </p>

            <p className="mt-4 max-w-2xl text-xs italic leading-relaxed text-muted-foreground">
              {today.disclaimer}
            </p>

            <div className="mt-8 flex flex-wrap items-center gap-3">
              <Link href={`/brief/${today.id}`}>
                <Button size="lg" className="rounded-full px-5 bg-foreground text-background hover:bg-foreground/90">
                  Read today&rsquo;s brief
                  <ArrowRight className="ml-1 h-4 w-4" />
                </Button>
              </Link>
              <Link href="/archive">
                <Button size="lg" variant="outline" className="rounded-full px-5">
                  <Calendar className="mr-1 h-4 w-4" />
                  Browse archive
                </Button>
              </Link>
            </div>
          </div>

          {/* Items preview panel */}
          <aside className="relative rounded-xl border border-border bg-card/85 p-6 shadow-[0_8px_30px_-12px_rgba(0,0,0,0.12)] backdrop-blur">
            <p className="kicker-coral">In this brief</p>
            <ol className="mt-4 space-y-4">
              {today.items.slice(0, 3).map((item, idx) => (
                <li key={item.headline} className="flex gap-3">
                  <span className="font-mono text-xs font-semibold text-coral pt-1">
                    {String(idx + 1).padStart(2, "0")}
                  </span>
                  <span className="text-sm leading-relaxed text-card-foreground">
                    {item.headline}
                  </span>
                </li>
              ))}
            </ol>
            {today.items.length > 3 && (
              <p className="mt-5 text-xs text-muted-foreground">
                + {today.items.length - 3} more in the full brief
              </p>
            )}
          </aside>
        </div>
      </div>
    </section>
  );
}

function RecentBriefs({ briefs }: { briefs: Brief[] }) {
  const recent = briefs.slice(1, 4);
  return (
    <section className="container py-16 md:py-20">
      <div className="flex items-end justify-between gap-4">
        <div>
          <p className="kicker-coral">Recently filed</p>
          <h2
            className="mt-2 font-display text-3xl font-semibold tracking-tight md:text-4xl"
            style={{ fontVariationSettings: '"opsz" 144' }}
          >
            Earlier this week
          </h2>
        </div>
        <Link
          href="/archive"
          className="hidden md:inline-flex items-center gap-1 text-sm font-medium text-foreground/70 hover:text-foreground"
        >
          See all <ArrowRight className="h-4 w-4" />
        </Link>
      </div>

      <div className="mt-8 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
        {recent.map((b) => (
          <BriefCard key={b.id} brief={b} />
        ))}
      </div>

      <div className="mt-8 md:hidden">
        <Link href="/archive">
          <Button variant="outline" className="w-full rounded-full">
            See full archive <ArrowRight className="ml-1 h-4 w-4" />
          </Button>
        </Link>
      </div>
    </section>
  );
}

function LatestEpisode() {
  const { episodes } = useEpisodes();
  const ep = episodes[0];
  if (!ep) return null;
  return (
    <section className="border-y border-border bg-secondary/40">
      <div className="container py-16 md:py-20">
        <div className="grid gap-10 lg:grid-cols-[1.1fr_1fr] lg:items-center">
          <div className="overflow-hidden rounded-xl border border-border bg-black shadow-lg">
            <div className="aspect-video w-full">
              <iframe
                src={`https://www.youtube-nocookie.com/embed/${ep.youtubeId}?rel=0`}
                title={ep.title}
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                allowFullScreen
                loading="lazy"
                className="h-full w-full"
              />
            </div>
          </div>

          <div>
            <p className="kicker-coral">Latest episode</p>
            <h2
              className="mt-2 font-display text-3xl font-semibold leading-tight tracking-tight md:text-4xl"
              style={{ fontVariationSettings: '"opsz" 144' }}
            >
              {ep.title}
            </h2>
            <p className="mt-2 text-sm text-muted-foreground">
              <span className="font-mono">EP. {String(ep.episodeNo).padStart(2, "0")}</span> · {ep.durationMinutes} min · with {ep.hosts.join(" & ")}
            </p>
            <p className="mt-5 max-w-prose leading-relaxed text-foreground/85">
              {ep.description}
            </p>
            <div className="mt-7">
              <Link href="/podcast">
                <Button variant="outline" className="rounded-full">
                  All episodes <ArrowRight className="ml-1 h-4 w-4" />
                </Button>
              </Link>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function FollowStrip() {
  return (
    <section className="container py-16 md:py-20">
      <div className="relative overflow-hidden rounded-2xl border border-border bg-card p-8 md:p-12">
        <div
          aria-hidden
          className="pointer-events-none absolute -right-20 -top-20 h-64 w-64 rounded-full bg-coral/15 blur-3xl"
        />
        <div className="relative grid gap-6 md:grid-cols-[1.2fr_1fr] md:items-center">
          <div>
            <p className="kicker-coral">Follow along on X</p>
            <h3
              className="mt-2 font-display text-2xl font-semibold leading-tight tracking-tight md:text-3xl"
              style={{ fontVariationSettings: '"opsz" 144' }}
            >
              One short post each weekday. No fluff, no fishy stuff.
            </h3>
            <p className="mt-3 text-sm text-muted-foreground">
              Follow @oddessentials on X for daily brief drops, podcast announcements, and the occasional crustacean joke.
            </p>
          </div>
          <div className="flex md:justify-end">
            <Button
              asChild
              size="lg"
              className="h-12 rounded-full bg-foreground px-6 text-background hover:bg-foreground/90"
            >
              <a
                href="https://x.com/oddessentials"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Follow @oddessentials on X (opens in new tab)"
              >
                {/* Official X mark — fills with currentColor so it inverts cleanly across light/dark themes. */}
                <svg
                  aria-hidden
                  viewBox="0 0 24 24"
                  className="h-4 w-4"
                  fill="currentColor"
                >
                  <path d="M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z" />
                </svg>
                Follow @oddessentials
              </a>
            </Button>
          </div>
        </div>
      </div>
    </section>
  );
}

export default function Home() {
  const { briefs } = useBriefs();
  return (
    <SiteLayout>
      <HeroBrief briefs={briefs} />
      <RecentBriefs briefs={briefs} />
      <LatestEpisode />
      <FollowStrip />
    </SiteLayout>
  );
}
