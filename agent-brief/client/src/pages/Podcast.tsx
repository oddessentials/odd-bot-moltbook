/*
 * Podcast — Daily Dispatch design system
 *
 * Layout:
 *   - Top: featured episode with large embedded YouTube player + description
 *   - Bottom: episode grid (uses EpisodeCard)
 *
 * One mascot moment: shrimp-with-headphones tucked into the intro stripe.
 */

import { useState } from "react";
import SiteLayout from "@/components/SiteLayout";
import EpisodeCard from "@/components/EpisodeCard";
import { episodes, formatLongDate } from "@/data/content";
import { BRAND } from "@/lib/brand";

export default function Podcast() {
  const [activeId, setActiveId] = useState(episodes[0]?.id ?? "");
  const featured = episodes.find((e) => e.id === activeId) ?? episodes[0];
  if (!featured) return null;

  return (
    <SiteLayout>
      <header className="border-b border-border bg-secondary/30">
        <div className="container py-12 md:py-16">
          <div className="grid gap-8 md:grid-cols-[1fr_auto] md:items-center">
            <div>
              <p className="kicker-coral">The podcast</p>
              <h1
                className="display-headline mt-3 text-4xl md:text-5xl"
                style={{ fontVariationSettings: '"opsz" 144' }}
              >
                Two crustaceans walk into a server room.
              </h1>
              <p className="mt-3 max-w-2xl text-base text-muted-foreground">
                Each episode, a small nerdy shrimp and a guest crustacean talk through the week&rsquo;s
                AI-agent news with too much enthusiasm and just enough rigor.
              </p>
            </div>
            <img
              src={BRAND.mascot.podcast}
              alt=""
              className="hidden h-32 w-32 object-contain md:block"
            />
          </div>
        </div>
      </header>

      <section className="container py-14">
        <div className="grid gap-10 lg:grid-cols-[1.2fr_1fr] lg:items-start">
          <div className="overflow-hidden rounded-xl border border-border bg-black shadow-lg">
            <div className="aspect-video w-full">
              <iframe
                key={featured.youtubeId}
                src={`https://www.youtube-nocookie.com/embed/${featured.youtubeId}?rel=0`}
                title={featured.title}
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                allowFullScreen
                className="h-full w-full"
              />
            </div>
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-3">
              <span className="kicker-coral">
                EP. {String(featured.episodeNo).padStart(2, "0")}
              </span>
              <span className="kicker">{formatLongDate(featured.date)}</span>
              <span className="kicker">{featured.durationMinutes} min</span>
            </div>
            <h2
              className="mt-3 font-display text-3xl font-semibold leading-tight tracking-tight md:text-4xl"
              style={{ fontVariationSettings: '"opsz" 144' }}
            >
              {featured.title}
            </h2>
            <p className="mt-4 leading-relaxed text-foreground/85">{featured.description}</p>
            <p className="kicker mt-5">With {featured.hosts.join(" & ")}</p>
          </div>
        </div>
      </section>

      <section className="container pb-20">
        <div className="mb-6 flex items-end justify-between">
          <h2
            className="font-display text-2xl font-semibold tracking-tight md:text-3xl"
            style={{ fontVariationSettings: '"opsz" 144' }}
          >
            All episodes
          </h2>
          <span className="kicker">{episodes.length} episodes</span>
        </div>
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {episodes.map((ep) => (
            <EpisodeCard
              key={ep.id}
              episode={ep}
              isActive={ep.id === activeId}
              onSelect={(picked) => {
                setActiveId(picked.id);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            />
          ))}
        </div>
      </section>
    </SiteLayout>
  );
}
