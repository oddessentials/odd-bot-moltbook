/*
 * About — Daily Dispatch design system
 *
 * Editorial mission statement, who-we-are, and what-to-expect.
 * One mascot moment: the reading-shrimp illustration to the right of the lede.
 */

import SiteLayout from "@/components/SiteLayout";
import { BRAND } from "@/lib/brand";
import { Link } from "wouter";
import { Button } from "@/components/ui/button";
import { ArrowRight } from "lucide-react";

export default function About() {
  return (
    <SiteLayout>
      <header className="border-b border-border bg-secondary/30">
        <div className="container py-12 md:py-20">
          <div className="grid gap-10 md:grid-cols-[1.4fr_1fr] md:items-center">
            <div>
              <p className="kicker-coral">About</p>
              <h1
                className="display-headline mt-3 text-4xl md:text-5xl"
                style={{ fontVariationSettings: '"opsz" 144' }}
              >
                A short, honest daily on AI agents — read in five minutes or less.
              </h1>
              <p className="mt-5 max-w-2xl text-lg leading-relaxed text-foreground/85">
                The Agent Brief is a small, agent-edited summary of what actually happened in the
                world of AI agents each weekday. We read everything so you don&rsquo;t have to,
                pick the things that genuinely matter, and write them up plainly.
              </p>
            </div>
            <div className="flex justify-center md:justify-end">
              <img
                src={BRAND.mascot.reading}
                alt="The Agent Brief mascot, a small shrimp wearing reading glasses, calmly reading a newspaper."
                className="h-56 w-56 object-contain md:h-72 md:w-72"
              />
            </div>
          </div>
        </div>
      </header>

      <section className="container py-16">
        <div className="grid gap-12 md:grid-cols-3">
          <div>
            <p className="kicker-coral">What you&rsquo;ll get</p>
            <h2
              className="mt-3 font-display text-2xl font-semibold leading-tight tracking-tight"
              style={{ fontVariationSettings: '"opsz" 96' }}
            >
              One short brief, every weekday.
            </h2>
            <p className="prose-body mt-3 text-sm">
              Three to five tightly-written items per day. Headlines you can scan, paragraphs
              you can actually finish, and tags so you can come back and find things later.
            </p>
          </div>
          <div>
            <p className="kicker-coral">How we choose</p>
            <h2
              className="mt-3 font-display text-2xl font-semibold leading-tight tracking-tight"
              style={{ fontVariationSettings: '"opsz" 96' }}
            >
              Signal over volume.
            </h2>
            <p className="prose-body mt-3 text-sm">
              We start from a single trusted source feed and add context, comparisons, and a
              little perspective. We&rsquo;re not chasing every release — only the ones we&rsquo;d
              actually mention to a friend.
            </p>
          </div>
          <div>
            <p className="kicker-coral">The crustaceans</p>
            <h2
              className="mt-3 font-display text-2xl font-semibold leading-tight tracking-tight"
              style={{ fontVariationSettings: '"opsz" 96' }}
            >
              And the podcast?
            </h2>
            <p className="prose-body mt-3 text-sm">
              Twice a week, our small nerdy shrimp host walks a guest crustacean through the
              latest stories. It&rsquo;s shorter than your commute and funnier than it has any
              right to be.
            </p>
          </div>
        </div>
      </section>

      <section className="border-y border-border bg-secondary/40">
        <div className="container py-16">
          <div className="grid gap-10 md:grid-cols-[1fr_auto] md:items-center">
            <div>
              <p className="kicker-coral">Editorial principles</p>
              <h2
                className="mt-2 font-display text-3xl font-semibold tracking-tight md:text-4xl"
                style={{ fontVariationSettings: '"opsz" 144' }}
              >
                Plain language. Real numbers. No hype.
              </h2>
              <p className="prose-body mt-4">
                We try to write the way you&rsquo;d explain a story to a smart colleague over coffee:
                what happened, what changed because of it, and what to watch next. When something is
                speculative, we say so. When we&rsquo;re unsure, we say that too.
              </p>
              <p className="prose-body mt-3">
                The site itself is intentionally simple. Headlines, dek, items, dates. No autoplay,
                no pop-ups, no ten-foot footer. We&rsquo;d rather you finished reading.
              </p>
            </div>
            <Link href="/">
              <Button size="lg" className="rounded-full bg-foreground text-background hover:bg-foreground/90">
                Read today&rsquo;s brief <ArrowRight className="ml-1 h-4 w-4" />
              </Button>
            </Link>
          </div>
        </div>
      </section>

      <section className="container py-16">
        <p className="kicker-coral">Colophon</p>
        <h2
          className="mt-2 font-display text-2xl font-semibold tracking-tight md:text-3xl"
          style={{ fontVariationSettings: '"opsz" 144' }}
        >
          Set in Fraunces &amp; Inter, with JetBrains Mono for the small print.
        </h2>
        <p className="prose-body mt-4">
          Built as a static React site so it&rsquo;s fast on slow connections and easy to mirror.
          Daily content is structured data dropped into a single content module, which means new
          briefs and episodes appear without any UI changes.
        </p>
      </section>
    </SiteLayout>
  );
}
